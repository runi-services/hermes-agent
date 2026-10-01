import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from mcp import ClientSession
from mcp.client.extension import NotificationBinding
from pydantic import BaseModel

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from tools import mcp_tool


class ChannelParams(BaseModel):
    content: str
    meta: dict = {}


@dataclass
class DummySession:
    bindings: list


def test_notification_registration_is_gated_unique_and_unload_safe(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager = PluginManager()
    owner = PluginContext(PluginManifest(name="owner"), manager)
    rival = PluginContext(PluginManifest(name="rival"), manager)
    monkeypatch.setattr(owner, "_mcp_allowlist", lambda _plugin: ["seed"])
    monkeypatch.setattr(rival, "_mcp_allowlist", lambda _plugin: ["seed"])
    seen = []

    async def callback(params):
        seen.append(params.content)

    registration = owner.register_mcp_notification_handler(
        "seed", "notifications/claude/channel", ChannelParams, callback
    )
    binding = manager.get_mcp_notification_handlers("seed")[0]
    assert isinstance(binding, NotificationBinding)

    with pytest.raises(ValueError, match="already registered"):
        rival.register_mcp_notification_handler(
            "seed", "notifications/claude/channel", ChannelParams, callback
        )
    with pytest.raises(PermissionError):
        PluginContext(PluginManifest(name="denied"), manager).register_mcp_notification_handler(
            "seed", "notifications/claude/channel", ChannelParams, callback
        )

    asyncio.run(binding.handler(ChannelParams(content="before")))
    assert seen == ["before"]
    registration.dispose()
    assert manager.get_mcp_notification_handlers("seed") == []
    asyncio.run(binding.handler(ChannelParams(content="after")))
    assert seen == ["before"]


def test_server_task_passes_typed_notification_to_existing_injector(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager = PluginManager()
    manager._gateway_message_injector = (object(), lambda content, role, key: True)
    context = PluginContext(PluginManifest(name="seed-bridge"), manager)
    monkeypatch.setattr(context, "_mcp_allowlist", lambda _plugin: ["seed"])
    monkeypatch.setattr(context, "_gateway_injection_allowed", lambda: True)
    delivered = []

    def fake_inject(**kwargs):
        delivered.append(kwargs)
        return True

    manager._gateway_message_injector = (object(), fake_inject)

    async def callback(params):
        assert context.inject_message(
            params.content, session_key="agent:main:a2a:dm:ctx-1340"
        )

    context.register_mcp_notification_handler(
        "seed", "notifications/claude/channel", ChannelParams, callback
    )
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    monkeypatch.setattr(mcp_tool, "ClientSession", ClientSession)

    task = object.__new__(mcp_tool.MCPServerTask)
    task.name = "seed"
    kwargs = task._make_notification_bindings()
    binding = kwargs["notification_bindings"][0]
    assert binding.method == "notifications/claude/channel"

    asyncio.run(binding.handler(
        ChannelParams(
            content='<channel source="plugin:seed:a2a-bridge">control</channel>',
            meta={"context_id": "ctx-1340"},
        )
    ))
    assert delivered == [
        {
            "plugin_id": "seed-bridge",
            "content": '<channel source="plugin:seed:a2a-bridge">control</channel>',
            "session_key": "agent:main:a2a:dm:ctx-1340",
        }
    ]
