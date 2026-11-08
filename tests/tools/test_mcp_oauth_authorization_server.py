"""Explicit AS selection must narrow discovery, never relax issuer checks (#132730)."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("mcp.client.auth.oauth2")

RESOURCE = "https://resource.example/mcp"
MULTITENANT = "https://login.microsoftonline.com/organizations/v2.0"
AUTHORITY = "https://login.microsoftonline.com/11111111-2222-4333-8444-555555555555/v2.0"
SCOPE = "api://fixture/Read"


def _metadata(issuer=AUTHORITY):
    return {"issuer": issuer, "authorization_endpoint": AUTHORITY + "/authorize",
            "token_endpoint": AUTHORITY + "/token", "registration_endpoint": AUTHORITY + "/register",
            "device_authorization_endpoint": AUTHORITY + "/devicecode",
            "grant_types_supported": ["authorization_code", "refresh_token", "urn:ietf:params:oauth:grant-type:device_code"],
            "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": True}


class _Servers:
    def __init__(self, httpx, issuer=AUTHORITY, resource=RESOURCE):
        self.httpx, self.issuer, self.resource, self.requests = httpx, issuer, resource, []

    def __call__(self, request):
        url = str(request.url)
        self.requests.append((request.method, url, request.headers.get("Authorization")))
        response = lambda status, body, **headers: self.httpx.Response(status, json=body, headers=headers, request=request)
        if url == RESOURCE:
            if request.headers.get("Authorization") == "Bearer synthetic-access":
                return response(200, {"ok": True})
            return response(401, {}, **{"WWW-Authenticate": 'Bearer resource_metadata="https://resource.example/.well-known/oauth-protected-resource"'})
        if url in {"https://resource.example/.well-known/oauth-protected-resource", "https://resource.example/.well-known/oauth-protected-resource/mcp"}:
            return response(200, {"resource": self.resource, "authorization_servers": [MULTITENANT], "scopes_supported": [SCOPE]})
        if url == MULTITENANT + "/.well-known/openid-configuration":
            return response(200, _metadata("https://login.microsoftonline.com/{tenantid}/v2.0"))
        if url == AUTHORITY + "/.well-known/openid-configuration":
            return response(200, _metadata(self.issuer))
        if url == AUTHORITY + "/register":
            return response(201, {"client_id": "synthetic-client", "redirect_uris": ["http://127.0.0.1:8420/callback"],
                                  "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        if url == AUTHORITY + "/devicecode":
            return response(200, {"device_code": "synthetic-device", "user_code": "TEST-ONLY", "verification_uri": "https://login.example/device", "expires_in": 30, "interval": 0.001})
        if url == AUTHORITY + "/token":
            return response(200, {"access_token": "synthetic-access", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "synthetic-refresh"})
        return response(404, {})


def _provider(tmp_path, monkeypatch, redirect_issuer=AUTHORITY, **config):
    from tools.mcp_oauth import _authorization_code_result
    from tools.mcp_oauth_manager import HermesMCPOAuthProvider
    from tools.mcp_oauth_provider import build_provider_kwargs, prepare_oauth_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg, storage = prepare_oauth_config("srv", RESOURCE, {"authorization_server": AUTHORITY, "scope": SCOPE, **config})
    kwargs = build_provider_kwargs(cfg, storage, ssh_proxy_hint=False)
    seen = {}

    async def redirect(url):
        seen["url"] = url
        seen["state"] = parse_qs(urlsplit(url).query)["state"][0]

    async def callback():
        return _authorization_code_result("synthetic-code", seen["state"], iss=redirect_issuer)

    kwargs.update(redirect_handler=redirect, callback_handler=callback)
    return HermesMCPOAuthProvider(server_name="srv", server_url=RESOURCE, **kwargs), seen


@pytest.mark.asyncio
async def test_configured_authority_completes_browser_flow_with_strict_tenant_issuer(tmp_path, monkeypatch):
    from tools.mcp_tool import sdk_httpx

    provider, seen = _provider(tmp_path, monkeypatch)
    httpx = sdk_httpx()
    servers = _Servers(httpx)
    async with httpx.AsyncClient(auth=provider, transport=httpx.MockTransport(servers)) as client:
        result = await client.get(RESOURCE)
    assert result.status_code == 200
    assert seen["url"].startswith(AUTHORITY + "/authorize?")
    assert str(provider.context.oauth_metadata.issuer) == AUTHORITY
    assert provider.context.auth_server_url == AUTHORITY
    assert not any(url.startswith(MULTITENANT) for _, url, _ in servers.requests)
    assert json.loads((tmp_path / "mcp-tokens/srv.client.json").read_text())["issuer"] == AUTHORITY
    assert json.loads((tmp_path / "mcp-tokens/srv.json").read_text())["hermes_issuer"] == AUTHORITY


@pytest.mark.asyncio
async def test_native_device_login_uses_same_pinned_authority_and_persists_binding(tmp_path, monkeypatch):
    from tools.mcp_oauth_device import login_device
    from tools.mcp_tool import sdk_httpx

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    httpx = sdk_httpx()
    servers = _Servers(httpx)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(servers), **kw))
    await login_device("srv", RESOURCE, {"authorization_server": AUTHORITY, "client_id": "synthetic-client", "scope": SCOPE})
    assert ("POST", AUTHORITY + "/devicecode", None) in servers.requests
    assert not any(url.startswith(MULTITENANT) for _, url, _ in servers.requests)
    assert json.loads((tmp_path / "mcp-tokens/srv.client.json").read_text())["issuer"] == AUTHORITY
    assert json.loads((tmp_path / "mcp-tokens/srv.json").read_text())["hermes_issuer"] == AUTHORITY


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["browser", "device", "prefetch"])
@pytest.mark.parametrize("issuer", [MULTITENANT, "https://login.microsoftonline.com/{tenantid}/v2.0",
                                     AUTHORITY.replace("11111111", "99999999"), "https://evil.example/v2.0",
                                     "https://login.microsoftonline.com", AUTHORITY + "/"])
async def test_pinned_authority_never_accepts_another_issuer(tmp_path, monkeypatch, flow, issuer):
    from tools.mcp_oauth_device import login_device
    from tools.mcp_tool import sdk_httpx

    provider, seen = _provider(tmp_path, monkeypatch)
    httpx = sdk_httpx()
    servers = _Servers(httpx, issuer=issuer)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(servers), **kw))
    with pytest.raises(Exception, match="issuer mismatch"):
        if flow == "device":
            await login_device("srv", RESOURCE, {"authorization_server": AUTHORITY, "client_id": "synthetic-client", "scope": SCOPE})
        elif flow == "prefetch":
            await provider._prefetch_oauth_metadata()
        else:
            async with httpx.AsyncClient(auth=provider) as client:
                await client.get(RESOURCE)
    assert not seen
    assert not any(method == "POST" or bearer for method, _, bearer in servers.requests)


@pytest.mark.parametrize("authority", ["", False, [AUTHORITY], "http://login.example/tenant", "/relative",
                                        "https://user:secret@login.example/tenant", "https://login.example/tenant?q=1",
                                        "https://login.example/tenant#fragment", "https://login.example:bad/tenant",
                                        "https://login.example/ten\nant", " https://login.example/tenant"])
def test_invalid_authority_rejected_before_storage_or_network(tmp_path, monkeypatch, authority):
    from tools.mcp_oauth_provider import prepare_oauth_config

    home = tmp_path / "oauth-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    with pytest.raises(ValueError, match="authorization_server"):
        prepare_oauth_config("srv", RESOURCE, {"authorization_server": authority})
    assert not home.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_issuer", [None, MULTITENANT])
@pytest.mark.parametrize("expires_in", [0, 3600])
async def test_pin_rejects_old_or_unbound_grants_before_any_request(tmp_path, monkeypatch, stored_issuer, expires_in):
    from mcp.client.auth.exceptions import OAuthFlowError
    from mcp.shared.auth import OAuthMetadata, OAuthToken
    from tools.mcp_tool import sdk_httpx

    provider, seen = _provider(tmp_path, monkeypatch)
    storage = provider.context.storage
    if stored_issuer:
        storage.bind_issuer(stored_issuer)
    await storage.set_tokens(OAuthToken(access_token="old-access", refresh_token="old-refresh", token_type="Bearer", expires_in=expires_in))
    storage.save_oauth_metadata(OAuthMetadata.model_validate(_metadata(stored_issuer or AUTHORITY)))
    original = (tmp_path / "mcp-tokens/srv.json").read_bytes()
    httpx = sdk_httpx()
    servers = _Servers(httpx)
    for _ in range(2):  # A failed initialization must not leave the SDK initialized on the next attempt.
        with pytest.raises(OAuthFlowError, match="authorization_server.*login"):
            async with httpx.AsyncClient(auth=provider, transport=httpx.MockTransport(servers)) as client:
                await client.get(RESOURCE)
    assert not servers.requests and not seen
    assert (tmp_path / "mcp-tokens/srv.json").read_bytes() == original


@pytest.mark.asyncio
async def test_pin_rediscovers_stale_metadata_before_refresh(tmp_path, monkeypatch):
    from mcp.shared.auth import OAuthMetadata, OAuthToken
    from tools.mcp_tool import sdk_httpx

    provider, seen = _provider(tmp_path, monkeypatch, client_id="synthetic-client")
    storage = provider.context.storage
    storage.bind_issuer(AUTHORITY)
    await storage.set_tokens(OAuthToken(access_token="old-access", refresh_token="old-refresh", token_type="Bearer", expires_in=0))
    metadata = _metadata(MULTITENANT)
    metadata["token_endpoint"] = "https://evil.example/token"
    storage.save_oauth_metadata(OAuthMetadata.model_validate(metadata))
    httpx = sdk_httpx()
    servers = _Servers(httpx)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(servers), **kw))
    async with httpx.AsyncClient(auth=provider) as client:
        assert (await client.get(RESOURCE)).status_code == 200
    assert not seen
    assert ("POST", AUTHORITY + "/token", None) in servers.requests
    assert not any(url.startswith((MULTITENANT, "https://evil.example")) for _, url, _ in servers.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["browser", "device", "prefetch"])
async def test_authority_pin_does_not_bypass_resource_identity(tmp_path, monkeypatch, flow):
    from mcp.client.auth.exceptions import OAuthFlowError
    from tools.mcp_oauth_device import login_device
    from tools.mcp_tool import sdk_httpx

    provider, seen = _provider(tmp_path, monkeypatch)
    httpx = sdk_httpx()
    servers = _Servers(httpx, resource="https://other.example/mcp")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(servers), **kw))
    with pytest.raises(OAuthFlowError, match="[Rr]esource"):
        if flow == "device":
            await login_device("srv", RESOURCE, {"authorization_server": AUTHORITY, "client_id": "synthetic-client"})
        elif flow == "prefetch":
            await provider._prefetch_oauth_metadata()
        else:
            async with httpx.AsyncClient(auth=provider) as client:
                await client.get(RESOURCE)
    assert not seen
    assert not any(method == "POST" or bearer for method, _, bearer in servers.requests)


def test_manager_rebuilds_provider_when_authority_is_changed_or_removed(tmp_path, monkeypatch):
    from tools import mcp_oauth
    from tools.mcp_oauth_manager import MCPOAuthManager

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(mcp_oauth, "_is_interactive", lambda: True)
    manager = MCPOAuthManager()
    cfg = {"cimd": False, "redirect_port": 0}
    first = manager.get_or_build_provider("srv", RESOURCE, cfg)
    cfg["authorization_server"] = AUTHORITY
    pinned = manager.get_or_build_provider("srv", RESOURCE, cfg)
    assert pinned is not first
    assert pinned._hermes_authorization_server == AUTHORITY
    assert manager.get_or_build_provider("srv", RESOURCE, cfg) is pinned
    del cfg["authorization_server"]
    unpinned = manager.get_or_build_provider("srv", RESOURCE, cfg)
    assert unpinned is not pinned
    assert unpinned._hermes_authorization_server is None


@pytest.mark.asyncio
async def test_pin_does_not_fall_back_to_guessed_refresh_endpoint(tmp_path, monkeypatch):
    from mcp.client.auth.exceptions import OAuthFlowError
    from mcp.shared.auth import OAuthToken
    from tools.mcp_tool import sdk_httpx

    provider, seen = _provider(tmp_path, monkeypatch, client_id="synthetic-client")
    storage = provider.context.storage
    storage.bind_issuer(AUTHORITY)
    await storage.set_tokens(OAuthToken(access_token="old-access", refresh_token="old-refresh", token_type="Bearer", expires_in=0))
    httpx = sdk_httpx()
    requests = []

    def unavailable(request):
        requests.append(request)
        return httpx.Response(404, request=request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(unavailable), **kw))
    for _ in range(2):
        with pytest.raises(OAuthFlowError, match="authorization_server"):
            async with httpx.AsyncClient(auth=provider) as client:
                await client.get(RESOURCE)
    assert not seen
    assert not any(request.method == "POST" or request.headers.get("Authorization") for request in requests)


@pytest.mark.asyncio
async def test_legacy_builder_never_guesses_a_pinned_refresh_endpoint(tmp_path, monkeypatch):
    from mcp.client.auth.exceptions import OAuthFlowError
    from mcp.shared.auth import OAuthToken
    from tools.mcp_oauth import build_oauth_auth

    provider, _ = _provider(tmp_path, monkeypatch, client_id="synthetic-client", cimd=False)
    storage = provider.context.storage
    storage.bind_issuer(AUTHORITY)
    await storage.set_tokens(OAuthToken(access_token="old-access", refresh_token="old-refresh", token_type="Bearer", expires_in=0))
    legacy = build_oauth_auth("srv", RESOURCE, {"authorization_server": AUTHORITY, "client_id": "synthetic-client", "cimd": False})
    await legacy._initialize()
    with pytest.raises(OAuthFlowError, match="authorization_server"):
        await legacy._refresh_token()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("client_id", [None, "synthetic-client"])
async def test_pin_refuses_first_login_without_metadata(tmp_path, monkeypatch, legacy, client_id):
    from mcp.client.auth.exceptions import OAuthFlowError
    from tools.mcp_oauth import build_oauth_auth
    from tools.mcp_tool import sdk_httpx

    cfg = {"authorization_server": AUTHORITY, "client_id": client_id, "cimd": False}
    provider, seen = _provider(tmp_path, monkeypatch, **cfg)
    if legacy:
        from tools import mcp_oauth
        monkeypatch.setattr(mcp_oauth, "_is_interactive", lambda: True)
        actual = build_oauth_auth("srv", RESOURCE, cfg)
        actual.context.redirect_handler = provider.context.redirect_handler
        actual.context.callback_handler = provider.context.callback_handler
        provider = actual
    httpx = sdk_httpx()
    servers = _Servers(httpx)

    def unavailable(request):
        if str(request.url) == RESOURCE:
            return servers(request)
        servers.requests.append((request.method, str(request.url), request.headers.get("Authorization")))
        return httpx.Response(404, request=request)

    for _ in range(2):
        with pytest.raises(OAuthFlowError, match="authorization_server"):
            async with httpx.AsyncClient(auth=provider, transport=httpx.MockTransport(unavailable)) as client:
                await client.get(RESOURCE)
    assert not seen
    assert not any(method == "POST" or bearer for method, _, bearer in servers.requests)


@pytest.mark.asyncio
async def test_pin_preserves_redirect_issuer_validation(tmp_path, monkeypatch):
    from mcp.client.auth.exceptions import OAuthFlowError
    from tools.mcp_tool import sdk_httpx

    provider, _ = _provider(tmp_path, monkeypatch, redirect_issuer=MULTITENANT)
    httpx = sdk_httpx()
    servers = _Servers(httpx)
    with pytest.raises(OAuthFlowError, match="iss mismatch"):
        async with httpx.AsyncClient(auth=provider, transport=httpx.MockTransport(servers)) as client:
            await client.get(RESOURCE)
    assert not any(url == AUTHORITY + "/token" for _, url, _ in servers.requests)
