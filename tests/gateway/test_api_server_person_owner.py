"""Offline own-door attribution regressions; never contact live seats or memory."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB

KEY = "offline-private-door-key"


@pytest.fixture
def seat(tmp_path):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": KEY}))
    db = SessionDB(db_path=tmp_path / "state.db")
    adapter._session_db = db
    adapter._run_agent = AsyncMock(return_value=(
        {"final_response": "offline fixture", "messages": [], "api_calls": 0},
        {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    ))
    yield adapter, db
    db.close()


def request(body, *, session: str | None = "fresh-body-talk", key=KEY):
    req = MagicMock(spec=web.Request)
    req.headers = {"Authorization": f"Bearer {key}"}
    if session is not None:
        req.headers["X-Hermes-Session-Id"] = session
    req.json = AsyncMock(return_value={
        "messages": [{"role": "user", "content": "offline fixture"}], **body,
    })
    return req


@pytest.mark.asyncio
async def test_private_body_person_reaches_agent_and_persists_fresh_owner(seat):
    adapter, db = seat
    response = await adapter._handle_chat_completions(request({"user_id": "person-a"}))
    assert response.status == 200
    assert adapter._run_agent.call_args.kwargs.get("user_id") == "person-a"
    assert db.get_session("fresh-body-talk")["user_id"] == "person-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,body", [
    ("person-a", {"user_id": "person-b"}),
    ("person-a", {}),
    (None, {"user_id": "person-a"}),
])
async def test_owner_mismatch_refused_before_history_or_agent(seat, monkeypatch, owner, body):
    adapter, db = seat
    db.create_session("fresh-body-talk", "api_server", user_id=owner)
    history = MagicMock(wraps=db.get_messages_as_conversation)
    monkeypatch.setattr(db, "get_messages_as_conversation", history)
    response = await adapter._handle_chat_completions(request(body))
    assert response.status == 409
    assert db.get_session("fresh-body-talk")["user_id"] == owner
    history.assert_not_called()
    adapter._run_agent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [None, "", " person-a", "person-a ", "person-a\n", "a\x00b", "a\x7fb", "a" * 257, 42, False, [], {}])
async def test_invalid_person_id_never_reaches_agent(seat, user_id):
    adapter, db = seat
    response = await adapter._handle_chat_completions(request({"user_id": user_id}))
    assert response.status == 400
    assert db.get_session("fresh-body-talk") is None
    adapter._run_agent.assert_not_called()


@pytest.mark.asyncio
async def test_user_id_requires_actual_private_key_not_an_alternate_auth_path(seat):
    adapter, db = seat
    adapter._check_auth = lambda req: None  # Offline model of a separate guest/session auth leg.
    response = await adapter._handle_chat_completions(request({"user_id": "person-a"}, session=None, key="guest-key"))
    assert response.status == 403
    assert db.get_session("fresh-body-talk") is None
    adapter._run_agent.assert_not_called()


@pytest.mark.asyncio
async def test_unkeyed_manual_listener_cannot_assert_a_person(seat):
    adapter, _ = seat
    adapter._api_key = ""
    response = await adapter._handle_chat_completions(request({"user_id": "person-a"}, session=None))
    assert response.status == 403
    adapter._run_agent.assert_not_called()


@pytest.mark.asyncio
async def test_anonymous_and_identified_first_writers_cannot_both_own_session(seat, monkeypatch):
    import asyncio
    import threading

    adapter, db = seat
    original = db.get_session
    barrier = threading.Barrier(2)
    local = threading.local()

    def simultaneous_first_read(session_id):
        row = original(session_id)
        if not getattr(local, "seen", False):
            local.seen = True
            barrier.wait(timeout=5)
        return row

    monkeypatch.setattr(db, "get_session", simultaneous_first_read)
    results = await asyncio.gather(
        adapter._handle_chat_completions(request({})),
        adapter._handle_chat_completions(request({"user_id": "person-a"})),
    )
    assert sorted(r.status for r in results) == [200, 409]
    assert adapter._run_agent.call_count == 1


@pytest.mark.asyncio
async def test_real_handler_executor_and_constructor_carry_person_before_memory(seat, monkeypatch):
    import types

    adapter, db = seat
    captured = {}

    class OfflineAgent:
        session_prompt_tokens = session_completion_tokens = session_total_tokens = 0

        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.session_id = kwargs["session_id"]

        def run_conversation(self, **kwargs):
            return {"final_response": "offline fixture", "messages": []}

    monkeypatch.setattr("run_agent.AIAgent", OfflineAgent)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {
        "provider": "openai", "api_key": "offline-fixture", "api_mode": "chat_completions",
    })
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "offline-model")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    monkeypatch.setattr("gateway.run.GatewayRunner._load_reasoning_config", staticmethod(lambda model="": {}))
    monkeypatch.setattr("gateway.run.GatewayRunner._load_fallback_model", staticmethod(lambda: None))
    monkeypatch.setattr("gateway.run._current_max_iterations", lambda: 1)
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *_: set())
    adapter._run_agent = types.MethodType(APIServerAdapter._run_agent, adapter)
    response = await adapter._handle_chat_completions(request({"user_id": "person-a"}))
    assert response.status == 200
    assert captured.get("user_id") == "person-a"
    assert captured["session_db"] is db
    assert db.get_session("fresh-body-talk")["user_id"] == "person-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("people", [("person-a", "person-b"), ("person-a", "person-a")])
async def test_cached_answer_cannot_cross_person_or_session(seat, monkeypatch, people):
    import json
    from gateway.platforms import api_server

    adapter, _ = seat
    monkeypatch.setattr(api_server, "_idem_cache", api_server._IdempotencyCache())

    async def answer(**kwargs):
        return {"final_response": kwargs["user_id"] + "@" + kwargs["session_id"], "messages": []}, {}

    adapter._run_agent.side_effect = answer
    for index, person in enumerate(people):
        req = request({"user_id": person}, session=f"talk-{index}")
        req.headers["Idempotency-Key"] = "same-offline-key"
        response = await adapter._handle_chat_completions(req)
        assert response.status == 200
        assert json.loads(response.body)["choices"][0]["message"]["content"] == f"{person}@talk-{index}"
    assert adapter._run_agent.call_count == 2


@pytest.mark.asyncio
async def test_fingerprint_sessions_are_separate_for_identified_people(seat):
    adapter, db = seat
    sessions = []
    for person in ["person-a", "person-b"]:
        response = await adapter._handle_chat_completions(request({"user_id": person}, session=None))
        assert response.status == 200
        session = adapter._run_agent.call_args.kwargs["session_id"]
        assert db.get_session(session)["user_id"] == person
        sessions.append(session)
    assert sessions[0] != sessions[1]


@pytest.mark.asyncio
async def test_streaming_private_person_uses_same_owner_gate(seat):
    from aiohttp.test_utils import TestClient, TestServer

    adapter, db = seat
    app = web.Application()
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/v1/chat/completions", headers={
            "Authorization": f"Bearer {KEY}", "X-Hermes-Session-Id": "offline-stream-talk",
        }, json={"user_id": "person-b", "stream": True,
                 "messages": [{"role": "user", "content": "offline fixture"}]})
        assert response.status == 200
        assert "[DONE]" in await response.text()
    assert adapter._run_agent.call_args.kwargs["user_id"] == "person-b"
    assert db.get_session("offline-stream-talk")["user_id"] == "person-b"


@pytest.mark.asyncio
async def test_owner_storage_failure_fails_closed(seat, monkeypatch):
    adapter, _ = seat
    monkeypatch.setattr(adapter, "_ensure_session_db_async", AsyncMock(side_effect=OSError("offline failure")))
    response = await adapter._handle_chat_completions(request({"user_id": "person-a"}))
    assert response.status == 503
    adapter._run_agent.assert_not_called()
