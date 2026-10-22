"""Pure profile-aware session-key parsing, shared by gateway notification paths."""
import re
from gateway.session import profile_from_session_key_namespace

_PROFILE_ID_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

def parse_session_key(session_key: str) -> dict | None:
    """Parse a session key (``agent:{ns}:{platform}:{chat_type}:{chat_id}[:{extra}...]``).

    ``{ns}`` is ``main`` for the default profile, ``main~`` for a profile literally named ``main``
    (``gateway.session._session_key_namespace``), or a named-profile id (profile ids match
    ``[a-z0-9][a-z0-9_-]{0,63}`` — never contain ``:`` — so a plain split stays unambiguous).
    For group/channel sessions the suffix may be a user_id, not a thread_id, so ``thread_id``
    is omitted. Named profiles are reported as ``profile``; ``main`` keys keep their historical
    shape exactly (no ``profile`` key) so equality assertions on parsed dicts stay stable.
    """
    parts = session_key.split(":")
    if (
        len(parts) >= 5
        and parts[0] == "agent"
        and (parts[1] in ("main", "main~") or _PROFILE_ID_KEY_RE.match(parts[1]))
    ):
        result = {"platform": parts[2], "chat_type": parts[3], "chat_id": parts[4]}
        if parts[1] != "main":
            result["profile"] = profile_from_session_key_namespace(parts[1])
        if parts[2:4] == ["a2a", "peer-dm"]:
            result["chat_type"] = "dm"
            result["scope_id"] = "peer"
        if len(parts) > 5 and result["chat_type"] in {"dm", "thread"}:
            result["thread_id"] = parts[5]
        return result
    return None
