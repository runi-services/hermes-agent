"""Bearer headers must hydrate from a private token_file, not only inline token."""

from __future__ import annotations

import os
import stat
import pytest

from plugins.platforms.a2a import protocol, tools


def _owner_only_token(tmp_path):
    token_path = tmp_path / "peer.token"
    token_path.write_text("fixture-peer-token-16b\n", encoding="utf-8")
    os.chmod(token_path, 0o600)
    return token_path


def _direct_peer_config(token_path):
    return {
        "a2a_agents": {
            "peer": {
                "url": "http://127.0.0.1:3979/?direct=1",
                "auth": {"type": "bearer", "token_file": str(token_path)},
            }
        }
    }


def _capture_send(monkeypatch):
    captured = {}

    def fake_post(url, body, headers, timeout):
        captured["url"] = url
        captured["headers"] = dict(headers)
        return protocol.jsonrpc_result(
            body["id"],
            protocol.build_task("t", "c1", protocol.STATE_COMPLETED, "PONG"),
        )

    monkeypatch.setattr(tools, "_http_get_json", lambda url, h, t: None)
    monkeypatch.setattr(tools, "_http_post_json", fake_post)
    return captured


def test_auth_header_present_from_owner_only_token_file(tmp_path):
    token_path = tmp_path / "peer.token"
    token_path.write_text("fixture-peer-token-16b\n", encoding="utf-8")
    os.chmod(token_path, 0o600)

    headers = tools._auth_header(
        {"type": "bearer", "token_file": str(token_path)}
    )

    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Bearer ")
    assert len(headers["Authorization"]) > len("Bearer ")


def test_auth_header_absent_when_token_file_is_group_readable(tmp_path):
    token_path = tmp_path / "peer.token"
    token_path.write_text("fixture-peer-token-16b\n", encoding="utf-8")
    os.chmod(token_path, stat.S_IRUSR | stat.S_IRGRP)

    # Reject instead of silently sending anonymously; this is a stronger boundary.
    with pytest.raises(ValueError, match="Configured A2A bearer file"):
        tools._auth_header({"type": "bearer", "token_file": str(token_path)})


def test_named_peer_token_file_hydrates_authorization(tmp_path, monkeypatch):
    token_path = tmp_path / "peer.token"
    token_path.write_text("fixture-peer-token-16b\n", encoding="utf-8")
    os.chmod(token_path, 0o600)
    monkeypatch.setattr(
        tools,
        "_load_config",
        lambda: {
            "a2a_agents": {
                "peer": {
                    "url": "http://127.0.0.1:3979/?direct=1",
                    "auth": {"type": "bearer", "token_file": str(token_path)},
                }
            }
        },
    )

    peer = tools._resolve_peer("peer")
    assert peer is not None
    headers = tools._auth_header(peer["auth"])

    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Bearer ")


def test_named_direct_send_carries_same_bearer_as_normal_path(tmp_path, monkeypatch):
    token_path = _owner_only_token(tmp_path)
    monkeypatch.setattr(tools, "_load_config", lambda: _direct_peer_config(token_path))
    captured = _capture_send(monkeypatch)

    out = tools.a2a_call({"agent": "peer", "message": "ping-direct"})

    assert "PONG" in out
    assert "direct=1" in captured["url"]
    assert captured["headers"].get("Authorization", "").startswith("Bearer ")
    assert len(captured["headers"]["Authorization"]) > len("Bearer ")


def test_direct_url_send_reuses_named_peer_bearer(tmp_path, monkeypatch):
    token_path = _owner_only_token(tmp_path)
    monkeypatch.setattr(tools, "_load_config", lambda: _direct_peer_config(token_path))
    captured = _capture_send(monkeypatch)

    out = tools.a2a_call(
        {"agent": "http://127.0.0.1:3979/?direct=1", "message": "ping-direct"}
    )

    assert "PONG" in out
    assert "direct=1" in captured["url"]
    assert captured["headers"].get("Authorization", "").startswith("Bearer ")
    assert len(captured["headers"]["Authorization"]) > len("Bearer ")


def test_unmatched_http_url_send_has_no_authorization(tmp_path, monkeypatch):
    token_path = _owner_only_token(tmp_path)
    monkeypatch.setattr(tools, "_load_config", lambda: _direct_peer_config(token_path))
    captured = _capture_send(monkeypatch)

    out = tools.a2a_call(
        {"agent": "http://127.0.0.1:3999/unmatched", "message": "ping-anon"}
    )

    assert "PONG" in out
    assert "Authorization" not in captured["headers"]
