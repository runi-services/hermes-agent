"""Service-authenticated channel admission and native personal-scope SSO.

Installed after App.initialize, before App observers. Personal activities only
complete a bounded server-created challenge; the original channel event is the
sole input to the normal gateway. Assertions never reach SDK activity observers.
"""
import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime
import json
import time
import weakref

from gateway.config import Platform
from gateway.delegated_authority import DelegatedAuthority, DelegatedDenied, partition, _fingerprint
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, build_session_key


@dataclass(repr=False)
class _Pending:
    event: MessageEvent
    route: object
    reference: object
    sender: str
    expires: float
    generation: int
    fingerprint: bytes
    personal_reference: object = None
    exchange_id: str = ""


class TeamsDelegatedIngress:
    def __init__(self, adapter, policy):
        from microsoft_teams.apps.auth import TokenValidator
        self.adapter = adapter
        self.policy = policy
        self.authority = DelegatedAuthority()
        self.pending = {}
        self.generations = {}
        self.seen = {}
        self.tasks = {}
        self._closed = False
        self.validators = {r.tenant_id: TokenValidator.for_entra(
            adapter._client_id, r.tenant_id, scope="access_as_user") for r in policy.routes}

    def install(self):
        from gateway.delegated_policy import validate_delegated_targets
        from tools.delegated_mcp import check_runtime
        check_runtime()
        validate_delegated_targets(self.adapter.gateway_runner.config)
        server = self.adapter._app.server
        if server._skip_auth or server._token_validator is None:
            raise ValueError("Protected Teams ingress requires SDK service authentication")
        server.on_request = self.on_request

    def _identity(self, activity):
        body = activity.model_dump(by_alias=True, exclude_none=True)
        conv, sender = body.get("conversation", {}), body.get("from", {})
        tenant = conv.get("tenantId")
        channel_tenant = body.get("channelData", {}).get("tenant", {}).get("id")
        if (body.get("channelId") != "msteams" or not tenant
                or (channel_tenant is not None and channel_tenant != tenant)
                or not sender.get("aadObjectId") or not sender.get("id")
                or body.get("recipient", {}).get("id") != "28:" + self.adapter._client_id
                or not body.get("id") or not body.get("serviceUrl") or not conv.get("id")):
            raise DelegatedDenied()
        return body, tenant, sender

    def _channel_occurrence(self, activity, body, tenant, sender):
        from microsoft_teams.api import ConversationReference
        conv = body["conversation"]
        if conv.get("conversationType") != "channel":
            raise DelegatedDenied()
        source = SessionSource(platform=Platform("teams"), chat_id=conv["id"],
            chat_type="channel", user_id=sender["aadObjectId"], guild_id=tenant,
            parent_chat_id=body.get("channelData", {}).get("channel", {}).get("id"), message_id=body["id"])
        source._transport_adapter_ref = weakref.ref(self.adapter)
        runner = self.adapter.gateway_runner
        route = self.policy.match(source, getattr(self.adapter, "_owner_profile", None))
        source.profile = runner._profile_name_for_source(source)
        if source.profile != route.profile or runner.config.delegated_routing is not self.policy:
            raise DelegatedDenied()
        source.delegated_session = True
        ref = ConversationReference(service_url=body["serviceUrl"], activity_id=body["id"],
            bot=body["recipient"], channel_id="msteams", conversation=conv, user=sender)
        return source, route, ref

    def _check_pending(self, pending):
        key = partition(pending.event.source)
        if (pending.expires <= time.monotonic() or self.generations.get(key) != pending.generation
                or _fingerprint(pending.event) != pending.fingerprint
                or self.adapter.gateway_runner.config.delegated_routing is not self.policy
                or self.adapter.gateway_runner._profile_name_for_source(pending.event.source) != pending.route.profile):
            raise DelegatedDenied()

    def _invalidate(self, key):
        self.generations[key] = self.generations.get(key, 0) + 1
        self.authority.invalidate_partition(key)
        self.pending.pop(key, None)
        task = self.tasks.pop(key, None)
        if task is not None:
            task.cancel()

    async def on_request(self, request):
        from microsoft_teams.api import InvokeResponse
        if self._closed:
            return InvokeResponse(status=403)
        try:
            if self.adapter._app.server._skip_auth:
                raise DelegatedDenied()
            body, tenant, sender = self._identity(request.body)
            now = time.monotonic()
            for key, pending in list(self.pending.items()):
                if pending.expires <= now:
                    self._invalidate(key)
            self.seen = {k: expiry for k, expiry in self.seen.items() if expiry > now}
            if body["conversation"].get("conversationType") == "personal":
                return await self._personal_exchange(body, tenant, sender)
            source, route, ref = self._channel_occurrence(request.body, body, tenant, sender)
            # Channel tokenExchange is not a supported authentication flow.
            if body["type"] != "message":
                raise DelegatedDenied()
            key = partition(source)
            occurrence = (key, body["id"])
            if occurrence in self.seen:
                return InvokeResponse(status=200)
            if key not in self.generations and len(self.generations) >= 4096:
                return InvokeResponse(status=429)
            self._invalidate(key)
            text = body.get("text", "")
            if not isinstance(text, str) or not text.strip() or len(text) > 16000 or body.get("attachments"):
                raise DelegatedDenied()
            if text.strip() in {"/stop", "/new", "/reset"}:
                if text.strip() != "/stop":
                    self.adapter.gateway_runner.session_store.reset_session(build_session_key(source, profile=source.profile))
                return InvokeResponse(status=200)
            if text.lstrip().startswith("/"):
                raise DelegatedDenied()
            if len(self.seen) >= 4096 or len(self.pending) >= 256:
                return InvokeResponse(status=429)
            self.seen[occurrence] = now + 600
            event = MessageEvent(text=text, source=source, message_id=body["id"], allow_gateway_control=False)
            pending = _Pending(event, route, ref, sender["id"], now + 180,
                               self.generations[key], _fingerprint(event))
            self.pending[key] = pending
            await self._sign_in(pending)
            return InvokeResponse(status=200)
        except asyncio.CancelledError:
            raise
        except Exception:
            # OAuth/HTTP exceptions can contain assertions, tokens or private data.
            return InvokeResponse(status=403)

    async def _sign_in(self, pending):
        from microsoft_teams.api import (ApiClient, CreateConversationParams, ConversationAccount,
            GetBotSignInResourceParams, TokenExchangeState, MessageActivityInput,
            OAuthCardAttachment, CardAction, CardActionType, card_attachment)
        from microsoft_teams.api.models.oauth import OAuthCard
        from microsoft_teams.common.http import ClientOptions
        app = self.adapter._app
        # Same native connector and bot; the service URL came through SDK service-JWT validation.
        api = ApiClient(pending.reference.service_url,
            app.http_client.clone(ClientOptions(token=app._get_bot_token)), app.options.api_client_settings)
        self._check_pending(pending)
        conversation = await api.conversations.create(CreateConversationParams(
            tenant_id=pending.route.tenant_id, members=[pending.reference.user]))
        self._check_pending(pending)
        if not conversation.id or conversation.id == pending.reference.conversation.id:
            raise DelegatedDenied()
        personal = pending.reference.model_copy(deep=True)
        personal.activity_id = None
        personal.conversation = ConversationAccount(id=conversation.id, tenant_id=pending.route.tenant_id,
                                                   conversation_type="personal", is_group=False)
        pending.personal_reference = personal
        state = TokenExchangeState(connection_name=pending.route.connection,
                                   conversation=personal, ms_app_id=app.id)
        resource = await app.api.bots.sign_in.get_resource(GetBotSignInResourceParams(
            state=base64.b64encode(json.dumps(state.model_dump()).encode()).decode()))
        self._check_pending(pending)
        if not resource.token_exchange_resource or not resource.token_exchange_resource.id:
            raise DelegatedDenied()
        pending.exchange_id = resource.token_exchange_resource.id
        card = MessageActivityInput(recipient=personal.user).add_attachments(card_attachment(
            attachment=OAuthCardAttachment(content=OAuthCard(text="Sign in to answer your channel request.",
                connection_name=pending.route.connection, token_exchange_resource=resource.token_exchange_resource,
                token_post_resource=resource.token_post_resource,
                buttons=[CardAction(type=CardActionType.SIGN_IN, title="Sign in", value=resource.sign_in_link)]))))
        await app.activity_sender.send(card, personal)
        self._check_pending(pending)

    async def _personal_exchange(self, body, tenant, sender):
        from microsoft_teams.api import InvokeResponse
        value = body.get("value", {})
        if body.get("type") != "invoke" or body.get("name") != "signin/tokenExchange":
            raise DelegatedDenied()
        candidates = [(key, p) for key, p in self.pending.items() if (
            p.personal_reference is not None and p.exchange_id
            and p.personal_reference.conversation.id == body["conversation"]["id"]
            and p.route.tenant_id == tenant and p.sender == sender["id"]
            and p.event.source.user_id == sender["aadObjectId"]
            and value.get("id") == p.exchange_id and value.get("connectionName") == p.route.connection
            and body["serviceUrl"] == p.reference.service_url)]
        if len(candidates) != 1:
            raise DelegatedDenied()
        key, pending = candidates[0]
        self._check_pending(pending)
        # Consume before validation/exchange awaits, while keeping the independent generation fence.
        del self.pending[key]
        await self._exchange(pending, value.get("token", ""))
        self._check_pending(pending)
        task = asyncio.create_task(self._run(pending))
        self.tasks[key] = task
        task.add_done_callback(lambda done: self.tasks.pop(key, None) if self.tasks.get(key) is done else None)
        return InvokeResponse(status=200)

    async def _exchange(self, pending, assertion):
        from microsoft_teams.api import ExchangeUserTokenParams, TokenExchangeRequest
        route = pending.route
        claims = await self.validators[route.tenant_id].validate_token(assertion)
        self._check_pending(pending)
        now = time.time()
        times = [claims.get(k) for k in ("nbf", "iat", "exp")]
        if (claims.get("aud") != self.adapter._client_id or claims.get("ver") != "2.0"
                or claims.get("tid") != route.tenant_id or claims.get("oid") != pending.event.source.user_id
                or claims.get("azp") not in route.client_ids
                or any(type(t) not in (int, float) for t in times)
                or not (times[0] <= now and times[1] <= now < times[2])
                or times[1] > times[2]):
            raise DelegatedDenied()
        response = await self.adapter._app.api.users.token.exchange(ExchangeUserTokenParams(
            connection_name=route.connection, user_id=pending.sender, channel_id="msteams",
            exchange_request=TokenExchangeRequest(token=assertion)))
        self._check_pending(pending)
        if response.connection_name != route.connection or response.channel_id != "msteams" or not response.expiration:
            raise DelegatedDenied()
        expiration = datetime.fromisoformat(response.expiration.replace("Z", "+00:00"))
        if expiration.tzinfo is None:
            raise DelegatedDenied()
        expiry = min(expiration.timestamp(), claims["exp"], time.time() + 300)
        # No await between the last generation/source check, authority issue and spawn.
        self.authority.issue(pending.event, route, response.token, expiry, assertion=assertion,
                             validator=lambda: self._check_route(pending))
        assertion = ""
        response = None

    def _check_route(self, pending):
        # Pending's consent TTL is distinct from the issued token's lifetime.
        if (self.generations.get(partition(pending.event.source)) != pending.generation
                or self.adapter.gateway_runner.config.delegated_routing is not self.policy
                or self.adapter.gateway_runner._profile_name_for_source(pending.event.source) != pending.route.profile):
            raise DelegatedDenied()

    async def _run(self, pending):
        event = pending.event
        try:
            self.authority.check(event)
            if not self.adapter._message_handler:
                return
            with self.authority.bind(event):
                response = await self.adapter._message_handler(event)
            self.authority.check(event)
            if response:
                from microsoft_teams.api import MessageActivityInput
                await self.adapter._app.activity_sender.send(
                    MessageActivityInput(text=str(response), reply_to_id=pending.reference.activity_id),
                    pending.reference)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # Fail closed; no credential-bearing exception payloads or resend.
        finally:
            self.authority.invalidate(event)

    async def close(self):
        self._closed = True
        for key in tuple(self.generations):
            self.generations[key] += 1
        self.authority.close()
        self.pending.clear()
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
