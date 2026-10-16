"""Native private peer regression; stdlib runner also works on installed source.

Run: python tests/plugins/test_a2a_private_sender.py [source-root]
All network peers and bearer values are disposable synthetic fixtures.
"""
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(sys.argv.pop(1)) if __name__ == '__main__' and len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
PKG = 'private_sender_test'
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(ROOT / 'plugins/platforms/a2a')]
sys.modules[PKG] = pkg
# Modern upstream uses this one coercion/secret seam; no real profile is imported.
shared = types.ModuleType('gateway.platforms._shared')
setattr(shared, 'coerce_port', lambda value, default: int(value or default))
setattr(shared, 'get_scoped_secret', lambda name, default='': default)
setattr(shared, 'profile_scoped', lambda: False)
previous_shared = sys.modules.get('gateway.platforms._shared')
sys.modules['gateway.platforms._shared'] = shared
for name in ['protocol', 'security', 'tools']:
    spec = importlib.util.spec_from_file_location(PKG + '.' + name, ROOT / 'plugins/platforms/a2a' / (name + '.py'))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
if previous_shared is None:
    sys.modules.pop('gateway.platforms._shared', None)
else:
    sys.modules['gateway.platforms._shared'] = previous_shared
tools = sys.modules[PKG + '.tools']
protocol = sys.modules[PKG + '.protocol']
security = sys.modules[PKG + '.security']

@unittest.skipUnless(os.name == 'posix', 'POSIX protected-file fixtures')
class PrivateSender(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.key = Path(self.tmp.name) / 'peer.token'
        self.key.write_text('fixture-directed-peer-bearer-0123456789\n')
        self.key.chmod(0o600)
        self.auth = {'type': 'bearer', 'token_file': str(self.key), 'token': 'stale-inline-0123456789'}

    def test_file_overrides_stale_inline_and_does_not_mutate_config(self):
        before = dict(self.auth)
        self.assertEqual(tools._auth_header(self.auth), {'Authorization': 'Bearer fixture-directed-peer-bearer-0123456789'})
        self.assertEqual(self.auth, before)

    def test_invalid_file_fails_closed_without_fallback(self):
        self.key.chmod(0o644)
        with self.assertRaisesRegex(ValueError, '^Configured A2A bearer file'):
            tools._auth_header(self.auth)

    def test_pinned_send_ignores_public_card_and_uses_file_at_actual_post(self):
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                captured.append(('GET', self.path))
                self.send_error(500)
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                captured.append(('POST', self.path, self.headers.get('Authorization')))
                result = protocol.jsonrpc_result(body['id'], protocol.build_task('receiver-owned-task', 'receiver-context', protocol.STATE_COMPLETED, 'house-answer'))
                self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(result).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True); t.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:%s/internal/' % server.server_port
        cfg = {'a2a': {'outbound_allowlist': ['peer']}, 'a2a_agents': {'peer': {'url': url, 'pin_url': True, 'auth': self.auth}}}
        card = {'url': 'https://parked.invalid/', 'supportedInterfaces': [{'protocolBinding': 'JSONRPC', 'url': 'https://parked.invalid/'}]}
        with patch.object(tools, '_load_config', return_value=cfg), patch.object(tools, '_fetch_card', return_value=card) as discovery, patch.object(protocol, 'persist_message'), patch.object(security, 'audit'):
            reply = tools.a2a_call({'agent': 'peer', 'message': 'Which unit is the Airbnb?'})
        self.assertIn('house-answer', reply)
        self.assertEqual(captured, [('POST', '/internal/', 'Bearer fixture-directed-peer-bearer-0123456789')])
        discovery.assert_not_called()

    def test_scope_blocks_arbitrary_url_and_other_named_peer_before_network(self):
        cfg = {'a2a': {'outbound_allowlist': ['peer']}, 'a2a_agents': {'peer': {'url': 'http://127.0.0.1:12345/', 'pin_url': True}, 'other': {'url': 'http://127.0.0.1:12346/'}}}
        with patch.object(tools, '_load_config', return_value=cfg), patch.object(tools, '_http_post_json') as post, patch.object(tools, '_fetch_card') as get:
            for target in ['other', 'http://127.0.0.1:12346/', 'https://parked.invalid/']:
                result = tools.a2a_call({'agent': target, 'message': 'no dispatch'})
                self.assertIn('Error:', result)
            result = tools.a2a_discover({'url': 'https://parked.invalid/'})
            self.assertIn('Error:', result)
        post.assert_not_called(); get.assert_not_called()

    def test_redirect_never_forwards_bearer_or_follows_post(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                seen.append(self.path)
                self.send_response(302); self.send_header('Location', '/leak'); self.end_headers()
            def do_POST(self):
                seen.append(self.path)
                self.send_response(307); self.send_header('Location', '/leak'); self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:%s/start' % server.server_port
        headers = {'Authorization': 'Bearer fixture-only'}
        for fn, args in [(tools._http_get_json, (url, headers, 2)), (tools._http_post_json, (url, {}, headers, 2))]:
            with self.assertRaises(Exception): fn(*args)
        self.assertEqual(seen, ['/start', '/start'])

    def test_invalid_file_shapes_never_dispatch(self):
        for shape in ['missing', 'symlink', 'hardlink', 'fifo', 'directory', 'oversized', 'unicode', 'empty']:
            with self.subTest(shape=shape):
                root = Path(self.tmp.name) / shape
                if shape == 'symlink': root.symlink_to(self.key)
                elif shape == 'hardlink': os.link(self.key, root)
                elif shape == 'fifo': os.mkfifo(root, 0o600)
                elif shape == 'directory': root.mkdir()
                elif shape != 'missing':
                    root.write_text({'oversized': 'a' * 5000, 'unicode': 'æ' * 40, 'empty': ''}.get(shape, ''))
                    root.chmod(0o600)
                with self.assertRaisesRegex(ValueError, '^Configured A2A bearer file'):
                    tools._auth_header(dict(self.auth, token_file=str(root)))
                if shape == 'hardlink': root.unlink()

    def test_orchestrate_rechecks_scope_at_dispatch(self):
        cfg = {'a2a': {'outbound_allowlist': ['peer']}, 'a2a_agents': {'other': {'url': 'http://127.0.0.1:12346/', 'pin_url': True}}}
        with patch.object(tools, '_load_config', return_value=cfg), patch.object(tools, '_http_post_json') as post, patch.object(tools, '_fetch_card') as get:
            result = tools.a2a_orchestrate({'capability': '*', 'message': 'must not send'})
        self.assertIn('Error:', result)
        post.assert_not_called(); get.assert_not_called()

    def test_unrestricted_inline_compatibility(self):
        self.assertEqual(tools._auth_header({'type': 'bearer', 'token': 'fixture-inline'}), {'Authorization': 'Bearer fixture-inline'})
        self.assertEqual(tools._auth_header({}), {})

class UnsupportedFileCustody(unittest.TestCase):
    def test_missing_secure_primitive_returns_sanitized_refusal(self):
        with patch.object(os, 'O_NOFOLLOW', None, create=True):
            with self.assertRaisesRegex(ValueError, '^Configured A2A bearer file is unreadable, insecure, or invalid$'):
                tools._auth_header({'type': 'bearer', 'token_file': '/never/read', 'token': 'no-fallback'})

if __name__ == '__main__': unittest.main(verbosity=2)
