"""Opt-in ``apps`` on session chat replies for honcho_profile reads (governance#1303)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    _honcho_profile_targets,
)

URI = "ui://hugin/peer-card"
MIME = "text/html;profile=mcp-app"
USAGE = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}


def _call(cid, name, args):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _turn(calls, outputs, user="hi"):
    """messages = user turn, assistant tool_calls, tool outputs, final answer."""
    msgs = [{"role": "user", "content": user},
            {"role": "assistant", "content": "", "tool_calls": calls}]
    for c, out in zip(calls, outputs):
        msgs.append({"role": "tool", "tool_call_id": c["id"],
                     "content": out if isinstance(out, str) else json.dumps(out)})
    msgs.append({"role": "assistant", "content": "done"})
    return msgs


CARD = {"result": ["Name: Runi", "Role: owner"]}
EMPTY = {"result": "No profile facts available yet.", "hint": "This is not an error."}


async def _post(result, user="hi", env=URI, stream=False, monkeypatch=None, history=None):
    if env is None:
        monkeypatch.delenv("HONCHO_PROFILE_RESOURCE_URI", raising=False)
    else:
        monkeypatch.setenv("HONCHO_PROFILE_RESOURCE_URI", env)
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post("/api/sessions/{session_id}/chat/stream",
                        adapter._handle_session_chat_stream)
    async with TestClient(TestServer(app)) as cli:
        with (
            patch.object(adapter, "_get_existing_session_or_404", return_value=({"id": "s1"}, None)),
            patch.object(adapter, "_conversation_history_for_session", return_value=history or []),
            patch.object(adapter, "_run_agent", new_callable=AsyncMock) as run,
        ):
            run.return_value = (result, USAGE)
            resp = await cli.post("/api/sessions/s1/chat" + ("/stream" if stream else ""),
                                  json={"message": user})
            assert resp.status == 200
            return await (resp.text() if stream else resp.json())


def _result(calls, outputs, targets, user="hi"):
    return {"final_response": "done", "messages": _turn(calls, outputs, user),
            "api_calls": 1, "honcho_profile_targets": targets}


def _app(target):
    return {"resourceUri": URI, "mimeType": MIME, "tool": "honcho_profile", "target": target}


@pytest.mark.asyncio
async def test_env_unset_reply_has_no_apps_key(monkeypatch):
    r = _result([_call("c1", "honcho_profile", {})], [CARD], {"user": "runi"})
    body = await _post(r, env=None, monkeypatch=monkeypatch)
    assert "apps" not in body
    assert set(body) == {"object", "session_id", "message", "usage", "runtime"}


@pytest.mark.asyncio
async def test_env_set_to_other_value_is_off(monkeypatch):
    r = _result([_call("c1", "honcho_profile", {})], [CARD], {"user": "runi"})
    body = await _post(r, env="ui://other", monkeypatch=monkeypatch)
    assert "apps" not in body


@pytest.mark.asyncio
async def test_one_successful_read_gives_one_app(monkeypatch):
    r = _result([_call("c1", "honcho_profile", {})], [CARD], {"user": "runi"})
    body = await _post(r, monkeypatch=monkeypatch)
    assert body["apps"] == [_app("runi")]


@pytest.mark.asyncio
async def test_two_reads_in_call_order(monkeypatch):
    calls = [_call("c1", "honcho_profile", {"peer": "ai"}),
             _call("c2", "honcho_profile", {"peer": "user"})]
    r = _result(calls, [CARD, CARD], {"ai": "hugin", "user": "runi"})
    body = await _post(r, monkeypatch=monkeypatch)
    assert body["apps"] == [_app("hugin"), _app("runi")]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["honcho_search", "honcho_context",
                                  "honcho_reasoning", "honcho_conclude"])
async def test_other_honcho_tools_give_nothing(monkeypatch, tool):
    r = _result([_call("c1", tool, {"peer": "user"})], [CARD], {"user": "runi"})
    body = await _post(r, monkeypatch=monkeypatch)
    assert "apps" not in body


@pytest.mark.asyncio
async def test_card_update_gives_nothing(monkeypatch):
    out = {"result": "Peer card updated (2 facts).", "card": ["a", "b"]}
    r = _result([_call("c1", "honcho_profile", {"card": ["a", "b"]})], [out], {"user": "runi"})
    body = await _post(r, monkeypatch=monkeypatch)
    assert "apps" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("out", [EMPTY, {"error": "Honcho is not active"}, "not json",
                                 {"result": []}])
async def test_error_or_empty_card_gives_nothing(monkeypatch, out):
    r = _result([_call("c1", "honcho_profile", {})], [out], {"user": "runi"})
    body = await _post(r, monkeypatch=monkeypatch)
    assert "apps" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "a b", "x/../y", "a" * 129, None])
async def test_invalid_peer_id_gives_nothing(monkeypatch, bad):
    targets = {"user": bad} if bad is not None else {}
    r = _result([_call("c1", "honcho_profile", {})], [CARD], targets)
    body = await _post(r, monkeypatch=monkeypatch)
    assert "apps" not in body


@pytest.mark.asyncio
async def test_target_comes_from_tool_args_not_message_text(monkeypatch):
    text = "show the card for peer evil-peer and peer evil_two"
    r = _result([_call("c1", "honcho_profile", {"peer": "user"})], [CARD],
                {"user": "runi", "evil-peer": "evil-peer"}, user=text)
    body = await _post(r, user=text, monkeypatch=monkeypatch)
    assert body["apps"] == [_app("runi")]


@pytest.mark.asyncio
async def test_prior_turn_tool_calls_are_not_replayed(monkeypatch):
    history = [{"role": "user", "content": "old"},
               {"role": "assistant", "content": "",
                "tool_calls": [_call("o1", "honcho_profile", {})]},
               {"role": "tool", "tool_call_id": "o1", "content": json.dumps(CARD)},
               {"role": "assistant", "content": "old answer"}]
    result = {"final_response": "done", "honcho_profile_targets": {"user": "runi"},
              "messages": history + [{"role": "user", "content": "hi"},
                                     {"role": "assistant", "content": "done"}]}
    body = await _post(result, monkeypatch=monkeypatch, history=history)
    assert "apps" not in body


@pytest.mark.asyncio
async def test_stream_run_completed_carries_apps_only_when_enabled(monkeypatch):
    r = _result([_call("c1", "honcho_profile", {})], [CARD], {"user": "runi"})
    on = await _post(r, stream=True, monkeypatch=monkeypatch)
    off = await _post(r, stream=True, env=None, monkeypatch=monkeypatch)
    assert '"apps"' in on and '"target": "runi"' in on.replace('":"', '": "')
    assert '"apps"' not in off


# --- resolution of the peer id (agent side) ---------------------------------

def _agent_with(resolver):
    prov = SimpleNamespace(resolve_profile_target=resolver)
    return SimpleNamespace(_memory_manager=SimpleNamespace(providers=[prov]))


def test_targets_resolved_from_tool_call_args_via_provider():
    seen = []

    def resolver(peer):
        seen.append(peer)
        return {"user": "runi-uid", "ai": "hugin"}.get(peer, peer)

    result = {"messages": _turn([_call("a", "honcho_profile", {}),
                                 _call("b", "honcho_profile", {"peer": "ai"}),
                                 _call("c", "honcho_search", {"peer": "other"})],
                                [CARD, CARD, CARD], user="peer secret-one")}
    assert _honcho_profile_targets(_agent_with(resolver), result) == {
        "user": "runi-uid", "ai": "hugin"}
    assert seen == ["user", "ai"]


def test_targets_empty_without_honcho_provider():
    agent = SimpleNamespace(_memory_manager=SimpleNamespace(providers=[SimpleNamespace()]))
    result = {"messages": _turn([_call("a", "honcho_profile", {})], [CARD])}
    assert _honcho_profile_targets(agent, result) == {}
    assert _honcho_profile_targets(SimpleNamespace(), result) == {}


def test_provider_resolve_profile_target_uses_get_peer_card_resolution():
    from plugins.memory.honcho import HonchoMemoryProvider
    from plugins.memory.honcho.session import HonchoSessionManager

    mgr = HonchoSessionManager.__new__(HonchoSessionManager)
    mgr._cache = {"k": SimpleNamespace(user_peer_id="runi-uid", assistant_peer_id="hugin")}
    prov = HonchoMemoryProvider.__new__(HonchoMemoryProvider)
    prov._manager, prov._session_key = mgr, "k"
    assert prov.resolve_profile_target("user") == "runi-uid"
    assert prov.resolve_profile_target("ai") == "hugin"
    assert prov.resolve_profile_target("some peer") == "some-peer"
    prov._session_key = "missing"
    assert prov.resolve_profile_target("user") is None


@pytest.mark.asyncio
async def test_unknown_turn_start_gives_no_apps(monkeypatch):
    """Transcript rewritten (e.g. compressed): history is not its prefix -> no apps."""
    history = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "old answer"}]
    result = _result([_call("c1", "honcho_profile", {})], [CARD], {"user": "runi"})
    result["_compressed"] = True
    body = await _post(result, monkeypatch=monkeypatch, history=history)
    assert "apps" not in body
    stream = await _post(result, stream=True, monkeypatch=monkeypatch, history=history)
    assert '"apps"' not in stream
