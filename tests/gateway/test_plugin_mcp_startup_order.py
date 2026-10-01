from __future__ import annotations

import asyncio


def test_gateway_loads_plugins_before_opening_mcp_sessions(monkeypatch) -> None:
    import gateway.run as gateway_run
    from hermes_cli import plugins as plugins_mod
    from tools import mcp_tool as mcp_mod

    events: list[str] = []
    monkeypatch.setattr(plugins_mod, "discover_plugins", lambda: events.append("plugins"))
    monkeypatch.setattr(mcp_mod, "discover_mcp_tools", lambda: events.append("mcp"))

    asyncio.run(gateway_run._discover_gateway_plugins_and_mcp())

    assert events == ["plugins", "mcp"]
