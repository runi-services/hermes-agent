"""Azure Live must use a path-only key and never the OpenAI credential chain."""
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools import voice_live


class AzureLiveTests(unittest.TestCase):
    def test_azure_session_uses_file_key_and_exact_endpoint(self):
        with TemporaryDirectory() as tmp:
            key = Path(tmp) / 'azure.key'
            key.write_text('azure-fixture-key\n')
            live = {'provider': 'azure', 'base_url': 'https://fixture.openai.azure.com/openai/v1',
                    'api_key_file': str(key), 'model': 'live-deployment', 'voice': 'stone'}
            captured = []
            def request(req, timeout):
                captured.append(req)
                return io.BytesIO(json.dumps({'session': {'id': 'live_fixture'},
                                             'transport': {'sdp': 'answer'}}).encode())
            with patch.object(voice_live, '_live_section', return_value=live), \
                 patch('tools.tool_backend_helpers.resolve_openai_audio_api_key', side_effect=AssertionError('OpenAI fallback forbidden')), \
                 patch.object(voice_live.urllib.request, 'urlopen', side_effect=request):
                result = voice_live.create_webrtc_session('offer\r\n')
            self.assertEqual(result['session']['id'], 'live_fixture')
            self.assertEqual(len(captured), 1)
            req = captured[0]
            self.assertEqual(req.full_url, 'https://fixture.openai.azure.com/openai/v1/live/sessions')
            self.assertEqual(req.get_header('Api-key'), 'azure-fixture-key')
            self.assertIsNone(req.get_header('Authorization'))
            body = json.loads(req.data)
            self.assertEqual(body['transport']['sdp'], 'offer\r\n')
            self.assertEqual(body['session']['model'], 'live-deployment')
            self.assertEqual(body['session']['delegation'], {'type': 'client'})

    def test_azure_failures_never_fall_back_or_send(self):
        with TemporaryDirectory() as tmp:
            empty = Path(tmp) / 'empty.key'
            empty.write_text('')
            baseline = {'provider': 'azure', 'base_url': 'https://fixture.openai.azure.com/openai/v1',
                        'api_key_file': str(Path(tmp) / 'missing.key')}
            invalid = [baseline, dict(baseline, api_key_file=''),
                       dict(baseline, api_key_file=str(empty)),
                       dict(baseline, base_url='https://api.openai.com/v1'),
                       dict(baseline, base_url=''), dict(baseline, provider='typo')]
            for live in invalid:
                with self.subTest(live=live), patch.object(voice_live, '_live_section', return_value=live), \
                     patch('tools.tool_backend_helpers.resolve_openai_audio_api_key', side_effect=AssertionError('OpenAI fallback forbidden')), \
                     patch.object(voice_live.urllib.request, 'urlopen', side_effect=AssertionError('network forbidden')):
                    with self.assertRaises(ValueError):
                        voice_live.create_webrtc_session('offer')

    def test_real_config_profiles_a_b_a_keep_their_azure_key_paths(self):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from hermes_cli.config import atomic_config_write
        with TemporaryDirectory() as tmp:
            homes = []
            for name in ('a', 'b'):
                home = Path(tmp) / name
                home.mkdir()
                key = home / 'azure.key'
                key.write_text('fixture-' + name)
                atomic_config_write(home / 'config.yaml', {'voice': {'voice_chat_mode': 'gpt-live',
                    'gpt_live': {'provider': 'azure', 'api_key_file': str(key),
                    'base_url': 'https://fixture.openai.azure.com/openai/v1'}}})
                homes.append(home)
            for home in [homes[0], homes[1], homes[0]]:
                token = set_hermes_home_override(home)
                try:
                    live = voice_live._live_section()
                    key, base = voice_live._resolve_credentials(live)
                    self.assertEqual(key, 'fixture-' + home.name)
                    self.assertEqual(voice_live.resolve_gpt_live_status()['provider'], 'azure')
                finally:
                    reset_hermes_home_override(token)

    def test_config_read_failure_cannot_select_openai(self):
        with patch('hermes_cli.config.load_config', side_effect=ValueError('unreadable config')), \
             patch('tools.tool_backend_helpers.resolve_openai_audio_api_key', side_effect=AssertionError('OpenAI fallback forbidden')), \
             patch.object(voice_live.urllib.request, 'urlopen', side_effect=AssertionError('network forbidden')):
            with self.assertRaisesRegex(ValueError, 'unreadable config'):
                voice_live.create_webrtc_session('offer')

    def test_status_reports_azure_key_failure_without_openai_fallback(self):
        voice = {'voice_chat_mode': 'gpt-live', 'gpt_live': {'provider': 'azure'}}
        with patch.object(voice_live, '_voice_section', return_value=voice):
            status = voice_live.resolve_gpt_live_status()
        self.assertEqual(status['provider'], 'azure')
        self.assertFalse(status['available'])
        self.assertIn('Azure', status['reason'])

    def test_openai_legacy_auth_is_unchanged(self):
        with patch.object(voice_live, '_live_section', return_value={'api_key': 'fixture-openai'}), \
             patch.object(voice_live.urllib.request, 'urlopen') as send:
            send.return_value.__enter__.return_value.read.return_value = b'{"session":{"id":"live_fixture"}}'
            voice_live.create_webrtc_session('offer')
        req = send.call_args.args[0]
        self.assertEqual(req.full_url, 'https://api.openai.com/v1/live/sessions')
        self.assertEqual(req.get_header('Authorization'), 'Bearer fixture-openai')
        self.assertIsNone(req.get_header('Api-key'))


if __name__ == '__main__':
    unittest.main()
