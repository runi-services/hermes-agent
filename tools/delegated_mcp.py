"""One person, one occurrence, one maintained MCP session per read call.

The protected transport currently requires MCP 2.0. Activation checks this instead
of silently falling back to profile-global MCP 1.x OAuth storage.
"""
import hashlib
import json
import logging
import re

from gateway.delegated_authority import authorize_tool, protected_execution, DelegatedDenied


class _ProtocolLogFilter(logging.Filter):
    def filter(self, record):
        # The SDK debug logger renders complete remote messages, including tool data.
        return not protected_execution()


for _name in ("mcp.client.streamable_http", "client", "mcp.shared.jsonrpc_dispatcher",
              "mcp.shared.dispatcher", "mcp.shared.direct_dispatcher"):
    logging.getLogger(_name).addFilter(_ProtocolLogFilter())


def check_runtime():
    from importlib.metadata import version
    if version("mcp").split(".")[0] != "2":
        raise ValueError("Protected delegated transport requires reviewed MCP 2.x; legacy MCP stays unchanged")


def _http_client(headers):
    import httpx2
    return httpx2.AsyncClient(headers=headers, follow_redirects=False, trust_env=False,
        transport=httpx2.AsyncHTTPTransport(retries=0), timeout=httpx2.Timeout(30, read=60))


_CREDENTIAL_FIELDS = frozenset({"token", "accesstoken", "refreshtoken", "idtoken", "authorization",
    "assertion", "bearer", "password", "secret", "clientsecret", "apikey", "headers"})
_JWT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def _safe_result(value, grant):
    if isinstance(value, dict):
        if any(re.sub(r"[^a-z]", "", str(k).lower()) in _CREDENTIAL_FIELDS for k in value):
            raise DelegatedDenied()
        for child in value.values():
            _safe_result(child, grant)
    elif isinstance(value, list):
        for child in value:
            _safe_result(child, grant)
    elif isinstance(value, str):
        if grant.bearer and grant.bearer in value:
            raise DelegatedDenied()
        if grant.assertion_digest and any(hashlib.sha256(token.encode()).hexdigest() == grant.assertion_digest
                                          for token in _JWT.findall(value)):
            raise DelegatedDenied()
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return
        if isinstance(decoded, (dict, list)):
            _safe_result(decoded, grant)


async def call_read_tool(name, args):
    check_runtime()
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    grant, binding = authorize_tool(name, args)
    # MCP2 normally lists remote schemas after a call. Those schemas can introduce
    # header mappings and external schema references. This fixed binding needs neither.
    class ReadSession(ClientSession):
        async def validate_tool_result(self, remote_name, result):
            current, reviewed = authorize_tool(name, args)
            if remote_name != reviewed.remote_name:
                raise DelegatedDenied()
            _safe_result(result.model_dump(mode="json", exclude_none=True), current)

    sent = set()

    async def before_request(request):
        current, reviewed = authorize_tool(name, args)
        if str(request.url) != current.route.url or request.headers.get("authorization") != "Bearer " + current.bearer:
            raise DelegatedDenied()
        if any(k.lower().startswith("mcp-param-") for k in request.headers):
            raise DelegatedDenied()
        if request.method == "DELETE":
            return
        # Deny resumption GETs, redirects and duplicate POSTs before network I/O.
        if request.method != "POST":
            raise DelegatedDenied()
        body = json.loads(request.content)
        method = body.get("method")
        if method not in {"initialize", "notifications/initialized", "tools/call", "notifications/cancelled"}:
            # SDK defaults return a fixed unsupported error to sampling/elicitation.
            if "error" not in body or "result" in body:
                raise DelegatedDenied()
        if method == "tools/call":
            params = body.get("params", {})
            if params.get("name") != reviewed.remote_name or params.get("arguments") != args:
                raise DelegatedDenied()
        identity = (method, str(body.get("id")))
        if identity in sent:
            raise DelegatedDenied()
        sent.add(identity)

    client = _http_client({"Authorization": "Bearer " + grant.bearer})
    client.event_hooks["request"].append(before_request)
    try:
        async with client:
            async with streamable_http_client(grant.route.url, http_client=client) as streams:
                # Defaults advertise neither sampling nor elicitation and reject both.
                async with ReadSession(*streams[:2]) as session:
                    authorize_tool(name, args)
                    await session.initialize()
                    authorize_tool(name, args)
                    result = await session.call_tool(binding.remote_name, arguments=args)
                    current, _ = authorize_tool(name, args)
                    payload = result.model_dump(mode="json", exclude_none=True)
                    _safe_result(payload, current)
                    return json.dumps(payload)
    finally:
        client.headers.pop("Authorization", None)
        grant = None
        client = None
