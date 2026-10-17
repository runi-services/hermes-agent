"""Native A2A session selection is independent of wire task/context identity."""
import asyncio
import json
from queue import Queue
import urllib.error
import urllib.request

import pytest

from gateway.config import GatewayConfig, PlatformConfig
from gateway.session import SessionStore, build_session_key
from plugins.platforms.a2a.adapter import A2AAdapter
from plugins.platforms.a2a import protocol


def test_peer_scope_routes_new_contexts_to_same_gateway_session():
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={"session_scope": "peer"}))
    events = Queue()

    async def receive(event):
        events.put(event)

    async def exercise():
        adapter._loop = asyncio.get_running_loop()
        adapter._message_handler = receive
        for ctx in ("lane-context", "new-context"):
            terminal, pending = await asyncio.to_thread(
                adapter._prepare_task,
                {"message": protocol.text_message(protocol.ROLE_USER, "follow up", context_id=ctx)},
                "peer-a",
            )
            assert terminal is None and pending is not None
            assert pending["context_id"] == ctx
        await asyncio.sleep(0)
        first, second = events.get_nowait(), events.get_nowait()
        assert build_session_key(first.source) == build_session_key(second.source)
        assert first.source.user_id == second.source.user_id == "peer-a"
        assert first.message_id != second.message_id

    asyncio.run(exercise())


def test_peer_replies_settle_only_the_exact_task():
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={"session_scope": "peer"}))
    chat_id = adapter._session_chat_id("peer-a", "old", adapter._agents[""])
    first = adapter._add_pending("task-a", chat_id)
    second = adapter._add_pending("task-b", chat_id)

    async def exercise():
        await adapter.send(chat_id, "answer b", reply_to="task-b", metadata={"notify": True})
        assert not first.done(), "reply for b must not settle the oldest task a"
        assert second.result(timeout=0) == (protocol.STATE_COMPLETED, "answer b")
        adapter._pop_pending("task-b")
        await adapter.send(chat_id, "late b", reply_to="task-b", metadata={"notify": True})
        await adapter.send(chat_id, "unbound", metadata={"notify": True})
        await adapter.send("wrong-chat", "wrong route", reply_to="task-a", metadata={"notify": True})
        await adapter.send(chat_id, "progress", reply_to="task-a")
        assert not first.done()
        await adapter.send(chat_id, "answer a", reply_to="task-a", metadata={"notify": True})
        assert first.result(timeout=0) == (protocol.STATE_COMPLETED, "answer a")

    asyncio.run(exercise())


@pytest.mark.parametrize("extra", [{}, {"session_scope": "context"}])
def test_context_scope_preserves_existing_routing_and_unbound_reply(extra):
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra=extra))
    agent = adapter._agents[""]
    assert adapter._session_chat_id("peer-a", "ctx/exact", agent) == "ctx/exact"
    assert adapter._session_chat_id("peer-b", "ctx/exact", agent) == "ctx/exact"
    future = adapter._add_pending("task-context", "ctx/exact")
    asyncio.run(adapter.send("ctx/exact", "legacy", metadata={"notify": True}))
    assert future.result(timeout=0) == (protocol.STATE_COMPLETED, "legacy")


@pytest.mark.parametrize("bad", ["peers", "", None, 1, [], {}])
def test_invalid_scope_refuses_startup(bad):
    with pytest.raises(ValueError, match="session_scope"):
        A2AAdapter(PlatformConfig(enabled=True, extra={"session_scope": bad}))


def test_peer_keys_are_stable_and_separate_exact_identities():
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={"session_scope": "peer"}))
    agent = adapter._agents[""]
    key = adapter._session_chat_id("a/b", "ctx", agent)
    assert key == adapter._session_chat_id("a/b", "different", agent)
    assert key != adapter._session_chat_id("a-b", "ctx", agent)
    assert key != adapter._session_chat_id("A/b", "ctx", agent)
    assert key != adapter._session_chat_id("a/b", "ctx", dict(agent, tenant="other"))
    assert key != adapter._session_chat_id("a/b", "ctx", dict(agent, slug="other"))
    adapter._active_profile = "other-profile"
    assert key != adapter._session_chat_id("a/b", "ctx", agent)


def test_real_http_auth_base_dispatch_session_store_and_task_reply(tmp_path, monkeypatch):
    """Real HTTP authentication -> native dispatch -> base final -> original task.

    Only the model is replaced. No live profiles, credentials, or peer sends.
    """
    monkeypatch.setenv("A2A_PEER_TOKENS", "peer-a:synthetic-peer-a,peer-b:synthetic-peer-b")
    monkeypatch.setenv("A2A_TRUSTED_PEERS", "peer-a,peer-b")
    monkeypatch.setenv("A2A_HOST", "127.0.0.1")
    monkeypatch.delenv("A2A_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("A2A_PORT", raising=False)
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={"session_scope": "peer", "port": 0}))
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    received = []

    async def model(event):
        entry = store.get_or_create_session(event.source)
        received.append((event.source.user_id, event.source.chat_id, entry.session_id, event.message_id))
        return f"ANSWER-{len(received)}"

    adapter.set_message_handler(model)

    def post(ctx, token):
        request = urllib.request.Request(
            f"http://127.0.0.1:{adapter._httpd.server_address[1]}/",
            data=json.dumps({"jsonrpc": "2.0", "id": "send", "method": "SendMessage", "params": {
                "message": protocol.text_message(protocol.ROLE_USER, "bounded proof", context_id=ctx)
            }}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    async def exercise():
        assert await adapter.connect()
        try:
            with pytest.raises(urllib.error.HTTPError) as error:
                await asyncio.to_thread(post, "denied", "wrong")
            assert error.value.code == 401
            assert received == []
            handles = []
            for ctx, token in (("first-context", "synthetic-peer-a"), ("new-context", "synthetic-peer-a"),
                               ("first-context", "synthetic-peer-b")):
                receipt = await asyncio.to_thread(post, ctx, token)
                task = receipt["result"].get("task", receipt["result"])
                handles.append(task["id"])
                assert task["contextId"] == ctx
                for _ in range(500):
                    record = adapter.tasks.get(task["id"])
                    if record["state"] == protocol.STATE_COMPLETED:
                        break
                    await asyncio.sleep(0.01)
                assert record["state"] == protocol.STATE_COMPLETED
                assert record["reply"] == f"ANSWER-{len(handles)}"
            assert len(set(handles)) == 3
            assert received[0][0] == received[1][0] == "peer-a"
            assert received[0][1:3] == received[1][1:3]
            assert received[2][0] == "peer-b"
            assert received[2][1] != received[0][1]
            assert received[2][2] != received[0][2]
        finally:
            await adapter.disconnect()

    asyncio.run(exercise())


@pytest.mark.parametrize("legacy_sender", ["peer-a", "peer-b"])
def test_peer_scope_never_adopts_legacy_context_session(tmp_path, legacy_sender):
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={"session_scope": "peer"}))
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    chat_id = adapter._session_chat_id("peer-a", "fresh-context", adapter._agents[""])
    legacy_source = adapter.build_source(chat_id=chat_id, chat_type="dm", user_id=legacy_sender)
    legacy = store.get_or_create_session(legacy_source)
    received = Queue()

    async def receive(event):
        received.put(event.source)

    async def exercise():
        adapter._loop = asyncio.get_running_loop()
        adapter._message_handler = receive
        terminal, pending = await asyncio.to_thread(adapter._prepare_task, {
            "message": protocol.text_message(protocol.ROLE_USER, "fresh", context_id="fresh-context")
        }, "peer-a")
        assert terminal is None and pending is not None
        await asyncio.sleep(0)
        source = received.get_nowait()
        current = store.get_or_create_session(source)
        assert current.session_id != legacy.session_id
        assert build_session_key(source) != build_session_key(legacy_source)
        assert current.origin.user_id == "peer-a"
        assert store.get_or_create_session(legacy_source).session_id == legacy.session_id
        assert legacy.origin.user_id == legacy_sender
        assert source.chat_type == "dm", "peer namespace must not change chat/queue semantics"

    asyncio.run(exercise())


def test_peer_namespace_roundtrips_native_key_parser():
    from gateway.run import _parse_session_key

    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={"session_scope": "peer"}))
    source = adapter.build_source(chat_id="stable-peer-id", chat_type="dm", user_id="peer-a", scope_id="peer")
    parsed = _parse_session_key(build_session_key(source))
    assert parsed is not None
    assert parsed["chat_type"] == "dm"
    assert parsed["scope_id"] == "peer"
    assert build_session_key(adapter.build_source(
        chat_id=parsed["chat_id"], chat_type=parsed["chat_type"], scope_id=parsed["scope_id"], user_id="peer-a"
    )) == build_session_key(source)


def test_background_source_fallback_retains_peer_namespace(monkeypatch):
    from types import SimpleNamespace
    from gateway.run import GatewayRunner
    from gateway.platform_registry import platform_registry

    # This unit fixture has not run gateway plugin discovery.
    monkeypatch.setattr(platform_registry, "is_registered", lambda platform: platform == "a2a")

    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={"session_scope": "peer"}))
    source = adapter.build_source(chat_id="stable-peer-id", chat_type="dm", user_id="peer-a", scope_id="peer")
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace(_ensure_loaded=lambda: None, _entries={})
    runner._get_cached_session_source = lambda key: None
    recovered = runner._build_process_event_source({"session_key": build_session_key(source)})
    assert recovered is not None
    assert recovered.scope_id == "peer"
    assert build_session_key(recovered) == build_session_key(source)
