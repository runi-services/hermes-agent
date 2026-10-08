"""Process-local occurrence custody shared by copied task and worker contexts.

Handles carry no credentials. Revocation removes the vault entry, so copying a
ContextVar never copies or revives authority. Only authenticated adapters issue.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, fields
import asyncio
import hashlib
from itertools import count
import json
import threading
import time
import weakref

from gateway.delegated_policy import DelegatedRoute


class DelegatedDenied(RuntimeError):
    def __init__(self):
        super().__init__("Delegated occurrence unavailable or tool denied")


@dataclass(frozen=True, repr=False, eq=False)
class Handle:
    pass


def _fingerprint(event):
    payload = {f.name: getattr(event, f.name) for f in fields(event)
               if not f.name.startswith("_") and f.name != "source"}
    payload["source"] = asdict(event.source)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).digest()


def partition(source):
    return hashlib.sha256(json.dumps([
        source.profile, source.guild_id, source.parent_chat_id, source.chat_id,
        source.user_id,
    ]).encode()).hexdigest()


@dataclass(repr=False)
class _Grant:
    event: object
    route: DelegatedRoute
    fingerprint: bytes
    bearer: str = field(repr=False)
    expiry: float
    deadline: float
    generation: int
    key: str
    assertion_digest: str = ""
    validator: object = None
    run_validator: object = None
    waiters: set = field(default_factory=set, repr=False)


_current = ContextVar("delegated_executor", default=None)
_authorities = weakref.WeakSet()
_issuances = count(1)


class DelegatedAuthority:
    def __init__(self):
        self._lock = threading.RLock()
        self._grants = {}
        self._generations = {}
        self._closed = False
        _authorities.add(self)

    def invalidate_partition(self, key):
        with self._lock:
            self._generations.pop(key, None)
            for handle, grant in list(self._grants.items()):
                if grant.key == key:
                    self._revoke(handle)

    def invalidate(self, event):
        with self._lock:
            self._revoke(getattr(event, "_delegated_handle", None))

    def _revoke(self, handle):
        # Caller holds the vault lock. Stored handles remain authoritative even
        # when an event's receipt was corrupted or replaced.
        grant = self._grants.pop(handle, None)
        if grant is not None:
            grant.bearer = ""
            if self._generations.get(grant.key) == grant.generation:
                self._generations.pop(grant.key, None)
            for loop, task in grant.waiters:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(task.cancel)

    def close(self):
        with self._lock:
            self._closed = True
            for handle in list(self._grants):
                self._revoke(handle)

    def issue(self, event, route, bearer, expiry, *, assertion="", validator=None):
        if not isinstance(bearer, str) or not bearer or any(c.isspace() for c in bearer) or expiry <= time.time():
            raise DelegatedDenied()
        key = partition(event.source)
        with self._lock:
            if self._closed:
                raise DelegatedDenied()
            self.invalidate_partition(key)
            self._generations[key] = next(_issuances)
            handle = Handle()
            event._delegated_handle = handle
            event.source.delegated_session = True
            self._grants[handle] = _Grant(event, route, _fingerprint(event), bearer, expiry,
                time.monotonic() + min(300, expiry - time.time()), self._generations[key], key,
                hashlib.sha256(assertion.encode()).hexdigest() if assertion else "", validator)
        return handle

    def check(self, event):
        with self._lock:
            grant = self._grants.get(getattr(event, "_delegated_handle", None))
            if (grant is None or grant.event is not event or grant.fingerprint != _fingerprint(event)
                    or grant.expiry <= time.time() or grant.deadline <= time.monotonic()
                    or grant.generation != self._generations.get(grant.key)):
                self.invalidate(event)
                raise DelegatedDenied()
            try:
                if grant.validator is not None:
                    grant.validator()
                if grant.run_validator is not None and not grant.run_validator():
                    raise DelegatedDenied()
            except Exception:
                self.invalidate(event)
                raise DelegatedDenied() from None
            return grant

    @contextmanager
    def bind(self, event):
        self.check(event)
        token = _current.set((self, event))
        try:
            yield
        finally:
            _current.reset(token)

    async def await_bound(self, event, action):
        """Keep reply work bound, deadline-limited and cancellable by any revoker."""
        with self.bind(event):
            waiter = (asyncio.get_running_loop(), asyncio.current_task())
            with self._lock:
                grant = self.check(event)
                grant.waiters.add(waiter)
            try:
                remaining = min(grant.expiry - time.time(), grant.deadline - time.monotonic())
                async with asyncio.timeout(max(0, remaining)):
                    return await action()
            finally:
                with self._lock:
                    grant.waiters.discard(waiter)


def current_grant():
    bound = _current.get()
    return bound[0].check(bound[1]) if bound is not None else None


def require_source_grant(source):
    grant = current_grant()
    if getattr(source, "delegated_session", False) is True or protected_execution():
        if grant is None or grant.event.source is not source:
            raise DelegatedDenied()
    return grant


def protected_execution():
    # Even a revoked context remains protected, and must never become legacy.
    return _current.get() is not None


def admit_before_hydration(runner, event):
    from gateway.delegated_policy import policy_for_source
    policy = policy_for_source(runner, event.source)
    if policy is None:
        return getattr(event, "_delegated_handle", None) is None
    try:
        adapter = runner._adapter_for_source(event.source)
        ingress = getattr(adapter, "_delegated_ingress", None)
        if ingress is None or ingress.policy is not policy:
            return False
        ingress.authority.check(event)
        return runner._profile_name_for_source(event.source) == event.source.profile
    except Exception:
        invalidate_events(event)
        return False


def invalidate_events(*events):
    for event in events:
        for authority in list(_authorities):
            authority.invalidate(event)


def invalidate_session(session_key):
    from gateway.session import build_session_key
    for authority in list(_authorities):
        with authority._lock:
            for grant in list(authority._grants.values()):
                if build_session_key(grant.event.source, profile=grant.event.source.profile) == session_key:
                    authority.invalidate(grant.event)


def bind_run_generation(validator):
    grant = current_grant()
    if grant is not None:
        grant.run_validator = validator


def bind_agent(agent):
    if protected_execution():
        agent._delegated_handle = current_grant().event._delegated_handle


def invalidate_agent(agent):
    handle = getattr(agent, "_delegated_handle", None)
    if handle is not None:
        for authority in list(_authorities):
            with authority._lock:
                grant = authority._grants.get(handle)
                if grant is not None:
                    authority.invalidate(grant.event)
        return True
    return False


def agent_context_missing(agent):
    handle = getattr(agent, "_delegated_handle", None)
    if handle is None:
        return False
    bound = _current.get()
    return bound is None or bound[1]._delegated_handle is not handle


def refuse_aggregate(*events):
    if any(getattr(event, "_delegated_handle", None) is not None for event in events):
        invalidate_events(*events)
        return True
    return False


_RESERVED = frozenset({"server", "url", "authority", "tenant", "tenantid", "person", "personid",
                      "account", "accountid", "token", "accesstoken", "bearer", "connection", "headers",
                      "authorization", "assertion", "profile", "clientid"})
_ESCAPES = frozenset({"tool_call", "tool_search", "tool_describe", "execute_code", "terminal",
                     "delegate_task", "send_message", "memory", "todo"})


def authorize_tool(name, args):
    grant = current_grant()
    if grant is None:
        raise DelegatedDenied()
    binding = next((t for t in grant.route.tools if t.name == name), None)
    if binding is None or name in _ESCAPES or not isinstance(args, dict):
        raise DelegatedDenied()

    def check_keys(value):
        if isinstance(value, dict):
            if any(str(k).lower().replace("_", "").replace("-", "") in _RESERVED for k in value):
                raise DelegatedDenied()
            for child in value.values():
                check_keys(child)
        elif isinstance(value, list):
            for child in value:
                check_keys(child)
    check_keys(args)
    from jsonschema import validate
    try:
        validate(args, json.loads(binding.schema_json))
    except Exception:
        raise DelegatedDenied() from None
    return grant, binding


def dispatch_protected(name, args):
    """None means legacy. Protected calls bypass all general hooks/bridges."""
    if not protected_execution():
        return None
    try:
        authorize_tool(name, args)
        from model_tools import _run_async
        from tools.delegated_mcp import call_read_tool
        return _run_async(call_read_tool(name, args))
    except Exception:
        # SDK/network exceptions can include request headers; never return or log them.
        return json.dumps({"error": "Delegated occurrence unavailable or tool denied"})
