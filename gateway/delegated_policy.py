"""Host-reviewed, opt-in routing and remote read-tool bindings.

Enabled policy is exclusive on the receiving Teams bot. Never infer a person,
channel, profile or authority from model arguments or an incomplete source.
"""
from dataclasses import dataclass
import json
import re
from urllib.parse import urlsplit
from uuid import UUID

from gateway.profile_routing import ProfileRouteRejected


def _text(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or "*" in value:
        raise ValueError("delegated_routing requires nonempty exact strings")
    return value


def _guid(value):
    value = _text(value)
    if str(UUID(value)) != value:
        raise ValueError("delegated_routing requires canonical GUIDs")
    return value


@dataclass(frozen=True)
class ReadBinding:
    name: str
    remote_name: str
    description: str
    schema_json: str

    def schema(self):
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": json.loads(self.schema_json)}}


@dataclass(frozen=True)
class DelegatedRoute:
    tenant_id: str
    channel_id: str
    person_ids: tuple[str, ...]
    profile: str
    server: str
    url: str
    connection: str
    tools: tuple[ReadBinding, ...]
    client_ids: tuple[str, ...] = ()
    bot_profile: str | None = None


@dataclass(frozen=True)
class DelegatedPolicy:
    routes: tuple[DelegatedRoute, ...]

    def protects(self, adapter_profile):
        return any(route.bot_profile == adapter_profile for route in self.routes)

    def to_dict(self):
        from dataclasses import asdict
        rows = []
        for route in self.routes:
            row = asdict(route)
            row["person_ids"] = list(route.person_ids)
            row["client_ids"] = list(route.client_ids)
            row["tools"] = [{"name": t.name, "remote_name": t.remote_name,
                "description": t.description, "input_schema": json.loads(t.schema_json)} for t in route.tools]
            rows.append(row)
        return {"enabled": True, "routes": rows}

    def match(self, source, adapter_profile=None):
        matches = [r for r in self.routes if (
            r.bot_profile == adapter_profile and r.tenant_id == source.guild_id
            and r.channel_id == source.parent_chat_id and source.user_id in r.person_ids
            and source.chat_type == "channel" and source.chat_id)]
        if len(matches) != 1:
            raise ProfileRouteRejected("Protected route denied")
        return matches[0]


def parse_delegated_policy(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict) or type(raw.get("enabled")) is not bool:
        raise ValueError("delegated_routing.enabled must be a boolean")
    if raw.get("enabled", False) is False:
        return None
    if set(raw) != {"enabled", "routes"} or not isinstance(raw["routes"], list) or not raw["routes"]:
        raise ValueError("enabled delegated_routing requires routes")
    routes = []
    required = {"tenant_id", "channel_id", "person_ids", "profile", "server", "url", "connection", "tools", "client_ids"}
    occupied = set()
    for row in raw["routes"]:
        if not isinstance(row, dict) or not required <= row.keys() or row.keys() - required - {"client_ids", "bot_profile"}:
            raise ValueError("invalid delegated route fields")
        values = {k: _text(row[k]) for k in required - {"person_ids", "tools", "client_ids"}}
        values["tenant_id"] = _guid(values["tenant_id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]+", values["profile"]):
            raise ValueError("invalid delegated profile")
        url = urlsplit(values["url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment or url.query:
            raise ValueError("delegated MCP URL must be a reviewed HTTPS endpoint without credentials")
        for key in ("person_ids", "client_ids"):
            items = row.get(key, [])
            if not isinstance(items, list) or not items:
                raise ValueError("invalid delegated identity list")
            values[key] = tuple(_guid(v) for v in items)
            if len(set(values[key])) != len(values[key]):
                raise ValueError("duplicate delegated identity")
        values["bot_profile"] = _text(row["bot_profile"]) if row.get("bot_profile") is not None else None
        bindings = []
        if not isinstance(row["tools"], list) or not row["tools"]:
            raise ValueError("delegated route requires exact reviewed read tools")
        for tool in row["tools"]:
            if not isinstance(tool, dict) or set(tool) != {"name", "remote_name", "description", "input_schema"}:
                raise ValueError("invalid delegated tool binding")
            name = _text(tool["name"])
            if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", name):
                raise ValueError("invalid delegated tool alias")
            schema = tool["input_schema"]
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise ValueError("delegated tool needs an object input schema")
            from jsonschema import Draft202012Validator
            Draft202012Validator.check_schema(schema)
            def no_refs(node):
                if isinstance(node, dict):
                    if "$ref" in node or "$dynamicRef" in node:
                        raise ValueError("delegated schemas must be self-contained without references")
                    for child in node.values():
                        no_refs(child)
                elif isinstance(node, list):
                    for child in node:
                        no_refs(child)
            no_refs(schema)
            bindings.append(ReadBinding(name, _text(tool["remote_name"]), _text(tool["description"]),
                                        json.dumps(schema, sort_keys=True)))
        if len({t.name for t in bindings}) != len(bindings) or len({t.remote_name for t in bindings}) != len(bindings):
            raise ValueError("duplicate delegated tool binding")
        values["tools"] = tuple(bindings)
        route = DelegatedRoute(**values)
        for person in route.person_ids:
            key = (route.bot_profile, route.tenant_id, route.channel_id, person)
            if key in occupied:
                raise ValueError("ambiguous delegated route")
            occupied.add(key)
        routes.append(route)
    return DelegatedPolicy(tuple(routes))


def resolve_delegated_profile(runner, source, adapter_profile=None):
    policy = getattr(getattr(runner, "config", None), "delegated_routing", None)
    if policy is None or source.platform.value != "teams":
        return None
    try:
        if adapter_profile is None:
            ref = getattr(source, "_transport_adapter_ref", None)
            owner = ref() if callable(ref) else None
            adapter_profile = getattr(owner, "_owner_profile", None)
        if not policy.protects(adapter_profile):
            return None
        if not runner.config.multiplex_profiles:
            raise ProfileRouteRejected("Protected routing requires explicitly enabled multiplexing")
        route = policy.match(source, adapter_profile)
        from gateway.run import _multiplex_profile_homes
        if route.profile not in {name for name, _ in _multiplex_profile_homes(runner.config)}:
            raise ProfileRouteRejected("Protected target is not served")
        return route.profile
    except Exception as exc:
        raise ProfileRouteRejected("Protected route denied") from exc


def policy_for_source(runner, source):
    policy = getattr(getattr(runner, "config", None), "delegated_routing", None)
    if policy is None or source.platform.value != "teams":
        return None
    ref = getattr(source, "_transport_adapter_ref", None)
    owner = ref() if callable(ref) else None
    if owner is None:
        # Unknown receiving transport is not proof of an unprotected primary bot.
        return policy
    return policy if policy.protects(getattr(owner, "_owner_profile", None)) else None


def validate_delegated_targets(config):
    if getattr(config, "delegated_routing", None) is None:
        return
    if not config.multiplex_profiles:
        raise ValueError("delegated_routing requires explicitly enabled multiplex_profiles")
    from gateway.run import _multiplex_profile_homes
    served = {name for name, _ in _multiplex_profile_homes(config)}
    if any(r.profile not in served for r in config.delegated_routing.routes):
        raise ValueError("delegated_routing target is not served")
