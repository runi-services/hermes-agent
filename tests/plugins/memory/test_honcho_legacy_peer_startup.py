"""Regression from a read-only legacy-server peer response (no live network)."""
from copy import deepcopy
import sys

import pytest
from pydantic import ValidationError

from plugins.memory.honcho.client import (
    HonchoClientConfig, get_honcho_client, reset_honcho_client,
)


@pytest.mark.parametrize("configuration", [
    {"observe_me": True, "observe_others": True},
    {"observe_me": True, "observe_others": False},
    {},
])
def test_client_startup_accepts_legacy_peer_response_without_changing_requests(
    monkeypatch, configuration,
):
    import honcho.api_types as api_types
    import honcho.client as sdk_client
    import tools.lazy_deps

    # The server's response shape, with synthetic identity and timestamp.
    response = {
        "id": "agent", "workspace_id": "test-workspace", "metadata": {},
        "created_at": "2026-09-26T16:37:00Z", "configuration": configuration,
    }
    original = deepcopy(response)
    refs = {name: getattr(module, "PeerResponse")
            for name, module in tuple(sys.modules.items())
            if (name == "honcho" or name.startswith("honcho."))
            and hasattr(module, "PeerResponse")}
    monkeypatch.setattr(tools.lazy_deps, "ensure", lambda *a, **kw: None)
    reset_honcho_client()
    try:
        client = get_honcho_client(HonchoClientConfig(
            api_key="test-key", workspace_id="test-workspace",
            base_url="http://127.0.0.1:1", timeout=1,
        ))
        calls = []

        def post(route, **kwargs):
            calls.append((route, kwargs))
            if route == "/v3/workspaces":
                return {"id": "test-workspace", "metadata": {}, "configuration": {}}
            assert route == "/v3/workspaces/test-workspace/peers"
            return deepcopy(response)

        monkeypatch.setattr(client._http, "post", post)
        peer = client.peer("agent")
        assert peer.id == "agent"
        assert [body for _, body in calls] == [
            {"body": {"id": "test-workspace"}}, {"body": {"id": "agent"}},
        ]
        assert response == original
        assert "observe_others" not in sdk_client.PeerResponse.model_validate(response).model_dump()["configuration"]
        with pytest.raises(ValidationError):
            api_types.PeerConfig.model_validate({"observe_others": True})
        with pytest.raises(ValidationError):
            sdk_client.PeerResponse.model_validate({
                **response, "configuration": {"unrelated_unknown_field": True},
            })
    finally:
        reset_honcho_client()
        for name, cls in refs.items():
            setattr(sys.modules[name], "PeerResponse", cls)
