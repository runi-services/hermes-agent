"""Receiver-byte proofs at the actual HTTP → gateway MessageEvent boundary."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import socket
from typing import Any
import urllib.request

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.a2a.adapter import A2AAdapter
from plugins.platforms.a2a import protocol


def _post(url, body, token="file-fixture-token") -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "A2A-Version": "1.0",
            "Authorization": "Bearer " + token,
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read().decode()
        if response.headers.get("Content-Type", "").startswith("text/event-stream"):
            return {
                "events": [
                    json.loads(line[6:])
                    for line in raw.splitlines()
                    if line.startswith("data: ")
                ]
            }
        return json.loads(raw)


def _adapter(monkeypatch, tmp_path, events):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("A2A_BEARER_TOKEN", "file-fixture-token")
    monkeypatch.delenv("A2A_PEER_TOKENS", raising=False)
    monkeypatch.delenv("A2A_TRUSTED_PEERS", raising=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("A2A_PORT", str(port))
    adapter = A2AAdapter(PlatformConfig(enabled=True))

    async def receive(event):
        events.append(event)
        await adapter.send(event.source.chat_id, "received", metadata={"notify": True})

    adapter.handle_message = receive
    adapter._message_handler = receive
    return adapter, f"http://127.0.0.1:{port}/"


def _body(part):
    return {
        "jsonrpc": "2.0",
        "id": "file-proof",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "sender-file-proof",
                "role": protocol.ROLE_USER,
                "parts": [{"text": "hash the received bytes"}, part],
            }
        },
    }


@pytest.mark.parametrize(
    "method", ["SendMessage", "SendStreamingMessage", "message/send", "message/stream"]
)
@pytest.mark.parametrize("legacy", [False, True])
def test_http_received_file_sha256_matches_sent_bytes(
    monkeypatch, tmp_path, method, legacy
):
    payload = bytes(range(256)) * 97 + b"\x00\xffreceived-file-proof\n"
    sent_sha256 = hashlib.sha256(payload).hexdigest()
    raw = base64.b64encode(payload).decode("ascii")
    part = (
        {
            "kind": "file",
            "file": {
                "bytes": raw,
                "name": "chain-picture.png",
                "mimeType": "image/png",
            },
        }
        if legacy
        else {"raw": raw, "filename": "chain-picture.png", "mediaType": "image/png"}
    )
    body = _body(part)
    body["method"] = method
    events = []
    adapter, url = _adapter(monkeypatch, tmp_path, events)

    async def run():
        assert await adapter.connect()
        try:
            response = await asyncio.to_thread(_post, url, body)
            assert "error" not in response
            for _ in range(100):
                if events:
                    break
                await asyncio.sleep(0.01)
            assert len(events) == 1
            event = events[0]
            assert len(event.media_urls) == 1, (
                "descriptor-only event loses the received bytes"
            )
            received = Path(event.media_urls[0])
            assert received.is_file()
            assert received.is_relative_to(tmp_path)
            assert hashlib.sha256(received.read_bytes()).hexdigest() == sent_sha256
            assert received.read_bytes() == payload
            assert event.media_types == ["image/png"]
            assert str(received) in event.text
            assert "bytes base64-encoded" not in event.text
            task_response = await asyncio.to_thread(
                _post,
                url,
                {
                    "jsonrpc": "2.0",
                    "id": "history",
                    "method": "GetTask",
                    "params": {"taskId": event.message_id, "historyLength": 20},
                },
            )
            history_part = task_response["result"]["history"][0]["parts"][1]
            assert history_part == part
            assert (
                hashlib.sha256(
                    base64.b64decode(
                        history_part["file"]["bytes"] if legacy else history_part["raw"]
                    )
                ).hexdigest()
                == sent_sha256
            )
        finally:
            await adapter.disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("token", ["", "wrong", "unprovisioned"])
def test_http_denied_credentials_create_no_files_or_tasks(monkeypatch, tmp_path, token):
    import urllib.error

    events = []
    adapter, url = _adapter(monkeypatch, tmp_path, events)
    body = _body({"raw": "aGVsbG8=", "filename": "hello.txt"})

    async def run():
        assert await adapter.connect()
        try:
            with pytest.raises(urllib.error.HTTPError) as denied:
                await asyncio.to_thread(_post, url, body, token)
            assert denied.value.code == 401
            assert not list(tmp_path.rglob("a2a-file-*"))
            assert adapter.tasks.list() == ([], 0)
            assert not events
        finally:
            await adapter.disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("raw", ["%%%", "aGVsbG8= garbage", "a", None, 42])
def test_http_bad_inline_file_rejected_without_text_only_dispatch(
    monkeypatch, tmp_path, raw
):
    events = []
    adapter, url = _adapter(monkeypatch, tmp_path, events)

    async def run():
        assert await adapter.connect()
        try:
            response = await asyncio.to_thread(_post, url, _body({"raw": raw}))
            task = protocol.unwrap_send_message_response(response["result"])
            assert task["status"]["state"] == protocol.STATE_REJECTED
            assert not events
            assert not list(tmp_path.rglob("a2a-file-*"))
        finally:
            await adapter.disconnect()

    asyncio.run(run())


def test_file_names_cannot_escape_or_overwrite_and_history_is_unchanged(tmp_path):
    import copy
    import stat
    from plugins.platforms.a2a import fileparts

    names = ["../../secret.txt", "..\\..\\secret.txt", "secret.txt", "...", "a" * 1000]
    message = {
        "message": {
            "parts": [
                {"raw": base64.b64encode(str(i).encode()).decode(), "filename": name}
                for i, name in enumerate(names)
            ]
        }
    }
    original = copy.deepcopy(message)
    text, paths, types = fileparts.materialize(message, home=str(tmp_path))
    assert message == original
    assert len(paths) == len(names)
    assert len(set(paths)) == len(names)
    for i, path in enumerate(paths):
        saved = Path(path)
        assert saved.is_relative_to(tmp_path / "cache" / "scratch")
        assert saved.read_bytes() == str(i).encode()
        assert stat.S_IMODE(saved.stat().st_mode) == 0o600
        assert stat.S_IMODE(saved.parent.stat().st_mode) == 0o700
        assert str(saved) in text
    assert types == ["application/octet-stream"] * len(names)
    _, later, _ = fileparts.materialize(message, home=str(tmp_path))
    assert set(paths).isdisjoint(later)


def test_empty_file_is_recoverable(tmp_path):
    from plugins.platforms.a2a import fileparts

    _, paths, _ = fileparts.materialize({"parts": [{"raw": ""}]}, home=str(tmp_path))
    assert Path(paths[0]).read_bytes() == b""


@pytest.mark.parametrize("case", ["count", "encoded", "aggregate", "late-invalid"])
def test_all_inline_files_validate_before_any_write(tmp_path, case):
    from plugins.platforms.a2a import fileparts

    if case == "count":
        parts = [{"raw": ""}] * (fileparts.MAX_FILES + 1)
    elif case == "encoded":
        parts = [{"raw": "A" * (4 * ((fileparts.MAX_BYTES + 2) // 3) + 1)}]
    elif case == "aggregate":
        raw = base64.b64encode(b"a" * (fileparts.MAX_BYTES // 2 + 1)).decode()
        parts = [{"raw": raw}, {"raw": raw}]
    else:
        parts = [{"raw": "aGVsbG8="}, {"raw": "%%%"}]
    with pytest.raises(fileparts.FilePartError):
        fileparts.materialize({"parts": parts}, home=str(tmp_path))
    assert not list(tmp_path.rglob("a2a-file-*"))


def test_inbox_write_failure_rolls_back_partial_files(tmp_path, monkeypatch):
    from plugins.platforms.a2a import fileparts

    real_open = fileparts.os.open
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("private filesystem detail must not escape")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(fileparts.os, "open", fail_second)
    with pytest.raises(
        fileparts.FilePartError, match="^Inline file inbox write failed$"
    ):
        fileparts.materialize({"parts": [{"raw": "aGVsbG8="}] * 2}, home=str(tmp_path))
    assert not list(tmp_path.rglob("a2a-file-*"))


def test_routed_profile_file_lives_in_receiver_home(tmp_path, monkeypatch):
    from plugins.platforms.a2a import adapter as adapter_module

    home = tmp_path / "routed-profile"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "gateway-profile"))
    monkeypatch.setattr(adapter_module, "_profile_home", lambda profile: str(home))
    instance = A2AAdapter(PlatformConfig(enabled=True))
    seen = []

    def forward(agent, peer, context_id, text):
        seen.append(text)
        return "received", protocol.STATE_COMPLETED

    monkeypatch.setattr(instance, "_forward_to_profile", forward)
    agent = {**instance._agents[""], "local": False, "profile": "routed-fixture"}
    terminal, pending = instance._prepare_task(
        _body({"raw": "aGVsbG8=", "filename": "hello.txt"})["params"],
        "fixture-peer",
        agent=agent,
    )
    assert terminal is None
    assert pending is not None
    pending["future"].result(timeout=3)
    paths = list(home.rglob("*-hello.txt"))
    assert len(paths) == 1
    assert paths[0].read_bytes() == b"hello"
    assert str(paths[0]) in seen[0]
    assert not list((tmp_path / "gateway-profile").rglob("a2a-file-*"))


def test_text_and_uri_reads_never_materialize_or_fetch(tmp_path, monkeypatch):
    from plugins.platforms.a2a import fileparts

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parts = [
        {"text": "ordinary"},
        {"url": "https://example.invalid/file.png", "filename": "file.png"},
    ]
    text, paths, types = fileparts.materialize({"parts": parts})
    assert "ordinary" in text and "https://example.invalid/file.png" in text
    assert paths == types == []
    protocol.extract_text({"parts": [{"raw": "aGVsbG8="}]})
    assert not list(tmp_path.rglob("a2a-file-*"))
