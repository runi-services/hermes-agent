"""Strict routes never turn an unmatched person into a default-profile turn."""

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.profile_routing import ProfileRouteRejected
from gateway.session import SessionSource


def policy():
    return {"enabled": True, "routes": [{
        "tenant_id": "11111111-1111-4111-8111-111111111111",
        "channel_id": "19:fixture-channel@thread.tacv2",
        "person_ids": ["22222222-2222-4222-8222-222222222222"],
        "profile": "fixture", "server": "workload", "url": "https://fixture.invalid/mcp",
        "connection": "workload", "client_ids": ["33333333-3333-4333-8333-333333333333"],
        "tools": [{"name": "read_fixture", "remote_name": "read_item",
                   "description": "Read an item", "input_schema": {"type": "object", "properties": {}}}],
    }]}


def test_unmatched_person_cannot_fall_back_to_active_profile():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig.from_dict({"multiplex_profiles": True, "delegated_routing": policy()})
    source = SessionSource(platform=Platform("teams"), chat_id="new-conversation",
                           chat_type="channel",
                           parent_chat_id=policy()["routes"][0]["channel_id"],
                           guild_id=policy()["routes"][0]["tenant_id"], user_id="unapproved-person")
    with pytest.raises(ProfileRouteRejected):
        runner._profile_name_for_source(source)


@pytest.mark.parametrize("mutation", ["missing_actor", "empty_actor", "enabled_string", "wildcard", "duplicate", "external_schema", "http_url"])
def test_malformed_enabled_policy_fails_real_config_load(tmp_path, monkeypatch, mutation):
    import yaml
    from gateway.config import load_gateway_config
    raw = policy()
    route = raw["routes"][0]
    if mutation == "missing_actor":
        del route["client_ids"]
    elif mutation == "empty_actor":
        route["client_ids"] = []
    elif mutation == "enabled_string":
        raw["enabled"] = "true"
    elif mutation == "wildcard":
        route["channel_id"] = "*"
    elif mutation == "duplicate":
        raw["routes"].append(dict(route))
    elif mutation == "external_schema":
        route["tools"][0]["input_schema"]["$ref"] = "https://unreviewed.invalid/schema"
    else:
        route["url"] = "http://fixture.invalid/mcp"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"gateway": {"delegated_routing": raw}}))
    with pytest.raises(ValueError):
        load_gateway_config()


def test_policy_roundtrip_served_targets_and_disabled_legacy(tmp_path, monkeypatch):
    import yaml
    from gateway.config import load_gateway_config
    from gateway.delegated_policy import validate_delegated_targets
    from gateway.run import GatewayRunner
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    raw = policy()
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"gateway": {"delegated_routing": raw}}))
    config = load_gateway_config()
    assert config.delegated_routing is not None and not config.multiplex_profiles
    with pytest.raises(ValueError):
        GatewayRunner(config)
    config.multiplex_profiles = True
    with pytest.raises(ValueError):
        GatewayRunner(config)
    # The real served-profile resolver observes an existing temp profile; the policy never enrolls it.
    (tmp_path / "profiles" / "fixture").mkdir(parents=True)
    from gateway.config import PlatformConfig
    config.platforms[Platform("teams")] = PlatformConfig(enabled=True)
    validate_delegated_targets(config)
    assert GatewayConfig.from_dict(config.to_dict()).delegated_routing == config.delegated_routing
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig.from_dict({"delegated_routing": {"enabled": False, "routes": "ignored"}})
    source = SessionSource(Platform("teams"), "legacy-chat", user_id="legacy-person")
    assert runner._profile_name_for_source(source) is None
    assert runner.config.to_dict() == GatewayConfig().to_dict()


def test_policy_only_claims_explicit_receiving_bots():
    import weakref
    from gateway.run import GatewayRunner
    from gateway.delegated_policy import policy_for_source
    from gateway.delegated_authority import admit_before_hydration
    from gateway.platforms.event import MessageEvent
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig.from_dict({"multiplex_profiles": True, "delegated_routing": policy()})
    class DedicatedAdapter:
        _owner_profile = "other-bot"
    adapter = DedicatedAdapter()
    source = SessionSource(Platform("teams"), "legacy", user_id="legacy-person")
    source._transport_adapter_ref = weakref.ref(adapter)
    runner._transport_owner = lambda source: (adapter, "other-bot")
    assert policy_for_source(runner, source) is None
    assert runner._profile_name_for_source(source) is None
    assert admit_before_hydration(runner, MessageEvent(text="legacy", source=source))


@pytest.mark.parametrize("data", [{"delegated_routing": None}, {"gateway": {"delegated_routing": None}}])
def test_explicit_null_policy_is_not_implicit_legacy(data):
    with pytest.raises(ValueError):
        GatewayConfig.from_dict(data)


def test_declared_policy_cannot_disappear_on_yaml_syntax_failure(tmp_path, monkeypatch):
    from gateway.config import load_gateway_config
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("gateway:\n  delegated_routing: [\n")
    with pytest.raises(ValueError):
        load_gateway_config()


@pytest.mark.parametrize("owner", ["default", "missing", "disabled", "unserved", None])
def test_receiving_bot_must_be_enabled_and_served_before_runner_hydration(tmp_path, monkeypatch, owner):
    from gateway.run import GatewayRunner
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    home = tmp_path / "home"
    (home / "profiles" / "disabled").mkdir(parents=True)
    (home / "profiles" / "disabled" / "config.yaml").write_text("platforms:\n  teams:\n    enabled: false\n")
    (home / "profiles" / "unserved").mkdir()
    (home / "profiles" / "unserved" / "config.yaml").write_text("platforms:\n  teams:\n    enabled: true\n")
    (home / "profiles" / ".deleted").mkdir()
    (home / "profiles" / ".deleted" / "unserved").touch()
    raw = policy()
    raw["routes"][0].update(profile="default", bot_profile=owner)
    config = GatewayConfig.from_dict({"multiplex_profiles": True, "delegated_routing": raw,
        "platforms": {"teams": {"enabled": owner is not None}}})
    with pytest.raises(ValueError, match="receiving"):
        GatewayRunner(config)


@pytest.mark.parametrize("owner", [None, "secondary"])
def test_receiving_bot_primary_and_served_secondary_are_valid(tmp_path, monkeypatch, owner):
    from gateway.delegated_policy import validate_delegated_targets
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    home = tmp_path / "home"
    (home / "profiles" / "secondary").mkdir(parents=True)
    (home / "profiles" / "secondary" / "config.yaml").write_text("platforms:\n  teams:\n    enabled: true\n")
    raw = policy()
    raw["routes"][0].update(profile="default", bot_profile=owner)
    config = GatewayConfig.from_dict({"multiplex_profiles": True, "delegated_routing": raw,
        "platforms": {"teams": {"enabled": True}}})
    validate_delegated_targets(config)
    assert config.delegated_routing.protects(owner)
