# Trusted private-body person attribution

This is a canonical harness receiver change, usable by every agent. It is not a per-seat adapter or a public login API.

`POST /v1/chat/completions` accepts optional top-level JSON `user_id` only when the request bears the receiving profile's actual private `API_SERVER_KEY`. A session/guest authentication path or an unkeyed listener cannot assert it. A trusted broker must derive the exact ID from its independently verified person; this field does not prove a client logged in and must never be copied from untrusted browser input. Public/guest senders omit the field; the body own door refuses a talk with no verified person before any POST.

Example private-body field: `"user_id": "person-a"`. This is not the OpenAI `user` field, metadata, a display name, an email-derived identity, or an instruction in conversation text. It is passed unchanged through `_run_agent` and `_create_agent` to `AIAgent(user_id=...)`, before memory initialization.

The native SessionDB row is claimed before history, agent creation, memory checkout, or idempotency replay. Its native upsert never changes an existing user_id; competing first writers are rechecked. Anonymous and identified writers cannot both acquire the same session. Existing NULL owners stay NULL and cannot be retroactively claimed. A supplied different person, or omission against an identified row, returns HTTP409 (`session_owner_mismatch`); use a fresh session. Unverifiable owner storage fails closed with HTTP503. Malformed IDs return400; alternate/unkeyed attribution returns403. No normalization, rebind, migration or historical peer repair is performed.

Both streaming and nonstreaming paths carry the person. Fingerprint-derived sessions are person-separated, and cached completions are profile/session/person separated. The 0.22.2 port retains the native credential-scoped replay namespace and validates both an explicit predecessor and its resolved continuation tip before reading stored history.

## Current-release port

`api-person-owner-0.22.2.patch` is the equivalent general harness source port, pinned to official commit `d923a5eb487a17c0a68801bba9ef0f56c81040a8`. Check exact base and `git apply --check` before any separately authorized installation. This artifact does not authorize activation, install a seat overlay, select a runtime source, or restart any agent. The canonical main change is the delivery source; the port records release-layout parity without downgrading a running0.22.2 seat to this fork's older main.

## Verification boundary

Offline tests use real native SessionDB and real handler/executor/constructor plumbing, with the model/agent-constructor seam replaced; no live memory or agent canary is sent. Live acceptance is a real authorized briefing and then the matching verified-person body talk, checking the persisted runtime user and the resolved Honcho peer. Sender transport and any one-session-per-talk policy belong to the body owner and retain their operator gates. Historical case-split peer repair is separate.
