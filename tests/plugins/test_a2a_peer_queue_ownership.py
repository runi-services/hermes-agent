"""Peer-scoped native admissions keep task ownership through the real busy path."""
import asyncio

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.a2a.adapter import A2AAdapter
from plugins.platforms.a2a import protocol


@pytest.mark.parametrize("control", ["normal", "split-flush", "cancel-b", "cancel-c", "failure"])
def test_running_a_and_queued_b_c_keep_all_reply_owners(control):
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={"session_scope": "peer"}))
    adapter._busy_text_mode = "queue"
    adapter._busy_text_debounce_seconds = 60
    adapter._busy_text_hard_cap_seconds = 60
    seen = []

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()
        adapter._loop = asyncio.get_running_loop()

        async def model(event):
            seen.append(event.text)
            if "INPUT-A" in event.text:
                entered.set()
                await release.wait()
                return "ANSWER-A"
            assert "INPUT-B" in event.text and "INPUT-C" in event.text
            if control == "failure":
                raise RuntimeError("bounded synthetic failure")
            return "ANSWER-BC"

        adapter.set_message_handler(model)

        async def admit(label):
            terminal, pending = await asyncio.to_thread(adapter._prepare_task, {
                "message": protocol.text_message(protocol.ROLE_USER, "INPUT-" + label, context_id="ctx-" + label)
            }, "peer-a")
            assert terminal is None and pending is not None
            return pending

        first = await admit("A")
        await asyncio.wait_for(entered.wait(), 3)
        second = await admit("B")
        if control == "split-flush":
            for _ in range(300):
                if adapter._text_debounce_store():
                    break
                await asyncio.sleep(.01)
            key = next(iter(adapter._text_debounce_store()))
            assert await adapter._flush_text_debounce_now(key)
        third = await admit("C")
        buffered = []
        for _ in range(300):
            buffered = list(adapter._text_debounce_store().values())
            if buffered and "INPUT-C" in buffered[0].event.text:
                break
            await asyncio.sleep(.01)
        assert len(seen) == 1, "queued tasks must not start a second writer"
        queued_text = "\n".join([state.event.text for state in buffered] +
                                [event.text for event in adapter._pending_messages.values()])
        assert "INPUT-B" in queued_text and "INPUT-C" in queued_text
        victim = second if control == "cancel-b" else third if control == "cancel-c" else None
        if victim:
            adapter._rpc_tasks_cancel("cancel", {"taskId": victim["task_id"]})
            assert victim["future"].result(timeout=0) == (protocol.STATE_CANCELED, "")
            adapter._finalize_task(victim, protocol.STATE_CANCELED, "")
            assert victim["task_id"] not in adapter._pending
        release.set()
        assert await asyncio.wait_for(asyncio.wrap_future(first["future"]), 3) == (protocol.STATE_COMPLETED, "ANSWER-A")
        awaited = second if victim is third else third
        await asyncio.wait_for(asyncio.wrap_future(awaited["future"]), 3)
        expected = ((protocol.STATE_FAILED, "[agent processing failed]") if control == "failure"
                    else (protocol.STATE_COMPLETED, "ANSWER-BC"))
        for pending in (second, third):
            assert pending["future"].done(), "consumed input lost its independent reply owner"
            assert pending["future"].result() == ((protocol.STATE_CANCELED, "") if pending is victim else expected)
        assert len(seen) == 2, "retain the existing queue coalescing policy"
        for pending in (first, second, third):
            state, reply = pending["future"].result()
            if pending is not victim:
                adapter._finalize_task(pending, state, reply)
            record = adapter.tasks.get(pending["task_id"])
            assert record is not None
            assert record["context_id"] == pending["context_id"]
            assert record["reply"] == reply
            assert record["state"] == state
        for _ in range(300):
            if not adapter._background_tasks:
                break
            await asyncio.sleep(.01)
        assert adapter._processing_reply_groups == {}

    asyncio.run(exercise())


def test_native_event_copy_retains_absorbed_reply_owners():
    from dataclasses import replace
    from plugins.platforms.a2a.adapter import A2APeerMessageEvent

    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={"session_scope": "peer"}))
    source = adapter.build_source(chat_id="peer-key", chat_type="dm", user_id="peer-a", scope_id="peer")
    first = A2APeerMessageEvent(text="one", message_id="task-one", source=source)
    second = A2APeerMessageEvent(text="two", message_id="task-two", source=source)
    first.absorb_reply_expected(second)
    first.message_id = second.message_id
    copied = replace(first, text="normalized turn")
    assert copied._reply_task_ids == ["task-one", "task-two"]
