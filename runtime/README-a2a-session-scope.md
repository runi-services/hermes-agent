# Native A2A session scope — 0.22.2 backport

`a2a-session-scope-0.22.2.patch` contains the equivalent native routing change for official release commit `d923a5eb487a17c0a68801bba9ef0f56c81040a8`. It is not an instruction to edit a running shared checkout or to restart any seat.

Apply only to a disposable copy of that exact release, with `git apply --check` first. Run the unchanged `tests/plugins/test_a2a_session_scope.py` from this PR there. The release retains its native security, protocol and task-lifetime semantics; this patch changes local session selection and final reply targeting only. The two implementations intentionally share the exact `_session_chat_id` method and final-send logic.

Acceptance measured before publication:
- Fork main focused A2A set: 175 passed.
- Fork broader set including phase23: 227 passed, 2 failed. Both failures reproduce on unchanged fork base `97ffb4d7cb38d1916682d3b89939fde38282b0c0`: anti-loop and latency tests expect synchronous SendMessage completion although that base now returns a working receipt. No assertions were weakened.
- Release backport plus native A2A tests: 171 passed, 1 skipped.
- Unchanged installed task-lifetime plugin suite against the backport: 65 passed (copied into the disposable test tree with its manifest and supporting files; synthetic unlisted-version allowance only).
- Real HTTP peer-scope/session-store proof with that plugin applied: 1 passed. Both named peers remain separate and both same-sender contexts settle their own tasks.
- New routing and wrong-task reply regressions each failed behaviorally before their implementation, then passed.

## Activation boundary

The operator explicitly ruled forward-only: **no rebind, no migration, no history merge**. Existing context-keyed sessions stay untouched. Prove a fresh lane using two same-sender messages with two different contexts and one receiver session ID. Do not claim receipt from HTTP admission alone; correlate both actual receiver user-message records to that session, plus task-bound replies at the sender.

Require an independent exact-head PASS and required checks before merge. Installation and activation are separate. The receiving seat's named drained minute must identify the actual gateway owner and shared-host scope, verify zero active work and unexpired leases immediately before activation, and preserve rollback. A stale PID/status file or idle nominated session does not prove the whole serving owner is drained. No live process, configuration, credential, allowlist, queue or session state is modified by this PR.

The currently installed task-lifetime plugin replaces `send()` at runtime. Its exact-task finals remain task-bound; its legacy unbound context fallback is outside this core patch and must be checked separately in the composed target before activation. Do not infer that core-only tests certify a plugin override.

## Independent-review correction

The first reviewed candidate was held: a hashed chat ID alone can equal a caller-controlled legacy context. The corrected candidate uses a fixed `peer-dm` session-key slot, set only from adapter-stamped scope metadata. Source chat type stays `dm`; native parser and background-source fallback preserve the scope. Both same-sender and different-sender seeded legacy-store regressions failed before the correction and now pass, without modifying the old rows. The release patch therefore now touches the adapter, session-key builder, key parser and extracted background-source fallback.

Latest corrected-source verification:
- Main focused A2A plus session/multiplex/Slack/background-routing tests: **294 passed**, four inherited Slack fixture coroutine warnings. An earlier run had 293 pass/1 fail in unchanged push-config HTTP CRUD; the same final set passed on rerun. The unchanged base comparison had 278 passed. This transient is retained, not relabelled as reproduced on base.
- Release equivalent focused set: **283 passed, 1 skipped**, two inherited Slack fixture coroutine warnings.
- Release with unchanged installed lifetime plugin suite plus composed peer-scope real HTTP proof: **66 passed**.
- The expanded async tests required the repository-pinned pytest-asyncio 1.3.0 in an isolated scratch support directory because the shared runtime venv lacks it. No runtime dependencies were installed or changed.
- These are focused test sets, not the full repository suite or a live seat acceptance.
