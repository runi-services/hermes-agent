"""Actual native busy admission preserves only the injected turn's task owners.

Model acceptance, authorization and lookup seams are synthetic. Native busy
routing, peer event, cancellation, final send and processing hooks are real.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageType, ProcessingOutcome, _reply_anchor_for_event
from gateway.run import GatewayRunner
from gateway.session import build_session_key
from plugins.platforms.a2a.adapter import A2AAdapter, A2APeerMessageEvent
from plugins.platforms.a2a import protocol


@pytest.mark.parametrize("mode", ["steer", "redirect"])
@pytest.mark.parametrize("control", ["normal", "cancel-first", "cancel-injected", "rejected"])
def test_native_busy_injection_retains_exact_task_owners(mode, control, monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={"session_scope": "peer"}))
    source = adapter.build_source(chat_id="peer-key", chat_type="dm", user_id="peer-a", scope_id="peer")
    first, incoming, unrelated = [A2APeerMessageEvent(
        text="input-" + label, message_type=MessageType.TEXT, source=source, message_id="task-" + label
    ) for label in ("first", "incoming", "unrelated")]
    futures = {}
    for event in (first, incoming, unrelated):
        futures[event.message_id] = adapter._add_pending(event.message_id, source.chat_id)
        adapter.tasks.create(event.message_id, "context-" + event.message_id, "peer-a")
    key = build_session_key(source)
    injected = []

    class Agent:
        _supports_active_turn_redirect = True

        def get_activity_summary(self):
            return {"seconds_since_activity": 0}

        def steer(self, text):
            if control == "rejected":
                return False
            injected.append(text)
            return True

        redirect = steer

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._draining = False
    runner._is_user_authorized = lambda source: True
    runner._is_user_authorized_for_source = lambda source: True
    runner._admit_bot_message_for_source = lambda source: True
    runner._effective_busy_input_mode = lambda source: "interrupt" if mode == "redirect" else "steer"
    runner._effective_busy_text_mode = lambda source: "interrupt"
    runner._adapter_for_source = lambda source: adapter
    runner._delivery_adapter_for = lambda source: adapter
    runner._route_plaintext_approval_while_busy = AsyncMock(return_value=False)
    state = SimpleNamespace(turn=SimpleNamespace(agent=Agent(), event=first, ctx=None))
    runner._peek_session_state = lambda key: state
    runner._session_state = lambda key: state
    runner._agent_has_active_subagents = lambda agent: False
    runner._session_has_compression_in_flight = AsyncMock(return_value=False)
    runner._prepare_busy_steer_text = AsyncMock(side_effect=lambda event: event.text)
    queued = []
    runner._queue_or_replace_pending_event = lambda key, event: queued.append(event)
    # A rejected redirect retains the existing interrupt-and-queue fallback.
    state.turn.agent.interrupt = lambda text: None

    async def exercise():
        await adapter.on_processing_start(first)
        assert await runner._handle_active_session_busy_message(incoming, key)
        accepted = control != "rejected"
        assert len(injected) == int(accepted)
        assert queued == ([] if accepted else [incoming])
        victim = first if control == "cancel-first" else incoming if control == "cancel-injected" else None
        if victim:
            adapter._rpc_tasks_cancel("cancel", {"taskId": victim.message_id})
            adapter._pop_pending(victim.message_id)
        actual = "actual running-turn final after " + (injected[0] if accepted else first.text)
        await adapter.send(source.chat_id, actual, reply_to=_reply_anchor_for_event(first), metadata={"notify": True})
        await adapter.on_processing_complete(first, ProcessingOutcome.SUCCESS)
        for event in (first, incoming):
            future = futures[event.message_id]
            if event is incoming and not accepted:
                assert not future.done(), "unconsumed queued input must not share the running final"
                continue
            assert future.done(), "accepted injected input lost its final owner"
            expected = (protocol.STATE_CANCELED, "") if event is victim else (protocol.STATE_COMPLETED, actual)
            assert future.result() == expected
        assert not futures[unrelated.message_id].done(), "same-peer pending does not imply turn ownership"
        assert adapter._processing_reply_groups == {}

    asyncio.run(exercise())


def test_native_turn_event_is_cleared_at_turn_release():
    from gateway.session_state import TurnState

    turn = TurnState()
    assert turn.event is None
    turn.event = object()
    turn.clear()
    assert turn.event is None


@pytest.mark.parametrize("mode", ["steer", "redirect"])
@pytest.mark.parametrize("path", ["busy", "priority"])
@pytest.mark.parametrize("control", ["normal", "cancel-first", "cancel-incoming"])
def test_native_message_claim_binds_injected_owners_and_releases_event(mode, path, control, monkeypatch):
    from unittest.mock import MagicMock
    from gateway.config import GatewayConfig
    import gateway.run as gateway_run

    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={"session_scope": "peer"}))
    event = A2APeerMessageEvent(text="claim this input", message_id="task-claim", source=adapter.build_source(
        chat_id="peer-claim", chat_type="dm", user_id="peer-a", scope_id="peer"))
    key = build_session_key(event.source)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {adapter.platform: adapter}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner._sessions = {}
    runner._session_db = None
    runner._draining = runner._external_drain_active = False
    runner._busy_input_mode = "interrupt"
    runner._is_user_authorized = lambda source: True
    runner._is_user_authorized_for_source = lambda source: True
    runner._admit_bot_message_for_source = lambda source: True
    runner._session_key_for_source = lambda source: build_session_key(source)
    runner._claim_active_session_slot = lambda *args: (None, None)
    runner._persist_active_agents = lambda: None
    runner._run_post_turn_hooks = AsyncMock()
    runner._set_session_env = lambda context: None
    runner._read_user_config = lambda: {}
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "synthetic"})
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    incoming = A2APeerMessageEvent(text="accepted follow-up", message_id="task-follow", source=event.source)
    unrelated = A2APeerMessageEvent(text="not consumed", message_id="task-other", source=event.source)
    futures = {}
    for item in (event, incoming, unrelated):
        futures[item.message_id] = adapter._add_pending(item.message_id, item.source.chat_id)
        adapter.tasks.create(item.message_id, "ctx-" + item.message_id, "peer-a")
    injected = []

    class Agent:
        _supports_active_turn_redirect = True

        def get_activity_summary(self):
            return {"seconds_since_activity": 0}

        def steer(self, text):
            injected.append(text)
            return True

        redirect = steer

    runner._effective_busy_input_mode = lambda source: "interrupt" if mode == "redirect" else "steer"
    runner._effective_busy_text_mode = lambda source: "interrupt"
    runner._agent_has_active_subagents = lambda agent: False
    runner._session_has_compression_in_flight = AsyncMock(return_value=False)
    runner._prepare_busy_steer_text = AsyncMock(side_effect=lambda item: item.text)
    runner._route_plaintext_approval_while_busy = AsyncMock(return_value=False)
    queued = []
    runner._queue_or_replace_pending_event = lambda key, item: queued.append(item)
    runner._delivery_adapter_for = lambda source: adapter
    visited = []
    actual = "actual synthetic final after accepted follow-up"

    async def model(claimed, *args):
        visited.append(claimed)
        assert runner._session_state(key).turn.event is event
        runner._session_state(key).turn.agent = Agent()  # synthetic model-constructor seam
        if path == "busy":
            assert await runner._handle_active_session_busy_message(incoming, key)
        else:
            assert await runner._handle_message(incoming) is None
        assert len(injected) == 1 and incoming.text in injected[0]
        assert queued == []
        victim = event if control == "cancel-first" else incoming if control == "cancel-incoming" else None
        if victim:
            adapter._rpc_tasks_cancel("cancel", {"taskId": victim.message_id})
            adapter._pop_pending(victim.message_id)
        return actual

    async def exercise():
        await adapter.on_processing_start(event)
        result = await runner._handle_message(event)
        assert result == actual
        await adapter.send(event.source.chat_id, result, reply_to=_reply_anchor_for_event(event), metadata={"notify": True})
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        for item in (event, incoming):
            canceled = (control == "cancel-first" and item is event) or (control == "cancel-incoming" and item is incoming)
            expected = (protocol.STATE_CANCELED, "") if canceled else (protocol.STATE_COMPLETED, actual)
            assert futures[item.message_id].done()
            assert futures[item.message_id].result() == expected
        assert not futures[unrelated.message_id].done()
        assert adapter._processing_reply_groups == {}

    runner._handle_message_with_agent = model
    asyncio.run(exercise())
    assert visited == [event]
    assert runner._session_state(key).turn.event is None
