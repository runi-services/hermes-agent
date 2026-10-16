# Native A2A session scope — 0.22.2 backport

`a2a-session-scope-0.22.2.patch` contains the equivalent adapter-only change for official release commit `d923a5eb487a17c0a68801bba9ef0f56c81040a8`. It is not an instruction to edit a running shared checkout or to restart any seat.

Apply only to a disposable copy of that exact release, with `git apply --check` first. Run the unchanged `tests/plugins/test_a2a_session_scope.py` from this PR there. The release retains its native security, protocol and task-lifetime semantics; this patch changes local session selection and final reply targeting only. The two implementations intentionally share the exact `_session_chat_id` method and final-send logic.

Acceptance measured before publication:
- Fork main focused A2A set: 175 passed.
- Fork broader set including phase23: 227 passed, 2 failed. Both failures reproduce on unchanged fork base `97ffb4d7cb38d1916682d3b89939fde38282b0c0`: anti-loop and latency tests expect synchronous SendMessage completion although that base now returns a working receipt. No assertions were weakened.
- Release backport plus native A2A tests: 171 passed, 1 skipped.
- Unchanged installed task-lifetime plugin suite against the backport: 65 passed (copied into the disposable test tree with its manifest and supporting files; synthetic unlisted-version allowance only).
- Real HTTP peer-scope/session-store proof with that plugin applied: 1 passed. Both named peers remain separate and both Abel contexts settle their own tasks.
- New routing and wrong-task reply regressions each failed behaviorally before their implementation, then passed.

## Activation boundary

Abel explicitly ruled forward-only in native context `ctx-5ad40c8df4374171`: **no rebind, no migration, no history merge**. Existing context-keyed sessions stay untouched. Prove a fresh lane using two Abel messages with two different contexts and one receiver session ID. Do not claim receipt from HTTP admission alone; correlate both actual receiver user-message records to that session, plus task-bound replies at Abel.

Single reviewer: Wolf, who did not write this diff. Merge only after his exact-head PASS and required checks. Installation and activation are separate. Wolf's named drained minute must identify the actual gateway owner and shared-host scope, verify zero active work and unexpired leases immediately before activation, and preserve rollback. A stale PID/status file or idle nominated session does not prove the whole serving owner is drained. No live process, configuration, credential, allowlist, queue or session state is modified by this PR.

The currently installed task-lifetime plugin replaces `send()` at runtime. Its exact-task finals remain task-bound; its legacy unbound context fallback is outside this core patch and must be checked separately in the composed target before activation. Do not infer that core-only tests certify a plugin override.
