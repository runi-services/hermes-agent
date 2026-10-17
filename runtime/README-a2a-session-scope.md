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

Namespace-correction verification (historical):
- Main focused A2A plus session/multiplex/Slack/background-routing tests: **294 passed**, four inherited Slack fixture coroutine warnings. An earlier run had 293 pass/1 fail in unchanged push-config HTTP CRUD; the same final set passed on rerun. The unchanged base comparison had 278 passed. This transient is retained, not relabelled as reproduced on base.
- Release equivalent focused set: **283 passed, 1 skipped**, two inherited Slack fixture coroutine warnings.
- Release with unchanged installed lifetime plugin suite plus composed peer-scope real HTTP proof: **66 passed**.
- The expanded async tests required the repository-pinned pytest-asyncio 1.3.0 in an isolated scratch support directory because the shared runtime venv lacks it. No runtime dependencies were installed or changed.
- These are focused test sets, not the full repository suite or a live seat acceptance.

## Queue ownership correction

The subsequent review reproduced loss of task B when the existing busy-text queue coalesced B/C and retained only C's message ID. The successor retains server-owned absorbed reply IDs on the native peer event. It uses the existing event-absorption hook, including debounce, and settles only those task owners with the coalesced turn's actual final. It retains the configured coalescing policy and does not restore FIFO final guessing. Cancellation stays exact-task; other absorbed tasks still receive their result when the surviving observer has already finalized. Event copies retain the owner list. The release baseline already provides the native debounce absorption hook; the successor does not add a redundant second call.

The real base-adapter A-running/B-and-C-queued regression failed on the old candidate before the fix. Controls cover normal merge, separate flushes into an occupied pending slot, either absorbed task cancelled/finalized, processing failure, native event copies and group cleanup. The changed implementation composes with the unchanged installed task-lifetime plugin: **71 passed** across its original suite plus real HTTP and five busy-queue controls. The release focused routing/session/queue set passed **293 with 1 skip** before the final event-copy guard. A queue-correction main broader run was explicitly non-green (**305 passed, 1 failed**) in unchanged push-config CRUD; a fresh unchanged-base execution reproduced that exact GET/configId KeyError on its first attempt. No existing assertion was weakened or skipped.

## Accepted busy-input correction

The next independent review cleared namespace and coalesced-queue ownership but held main's accepted steer/redirect paths: the older main caller never folded an injected task into the active turn. The exact release already has that native fold. Main now retains the event on its real `TurnState`, binds it at the existing pre-await turn claim, clears it at release, and folds only successful injections while the same agent still owns the turn. This covers the ordinary busy callback, priority paths and accepted `/steer`; no rejected input is folded or replayed.

Reply ownership remains server-owned but is deliberately **not frozen at turn start**: successful later injection extends the active list. Final resolution finds exact recorded membership, including a redirected reply anchor after either task observer has finalized. This preserves the actual final content rather than substituting empty success for the remaining injected task. Cancellation still resolves exactly one task.

The new 21-case file includes eight direct native-mode controls, twelve actual `GatewayRunner._handle_message` claim → accepted busy/priority injection → release controls, and the real turn-record cleanup test. Both cancellation directions and unrelated-task no-settlement are asserted. At predecessor `da864d513494abdf76fa9b5f8460e76a2f454842`, those unchanged tests produced **17 failures / 4 passes**; the four original counterexamples reach missing final ownership, and thirteen reach the genuinely missing turn-event binding/field. Model/provider acceptance and authorization decisions remain synthetic fixture seams, not live-provider consumption evidence.

Latest successor verification:
- Main expanded native A2A, session/routing, busy-policy/auth and turn-cleanup focused set: **362 passed**, four inherited Slack fixture coroutine warnings.
- Paired release native routing/session/queue/busy set: **315 passed, 1 skipped**, two inherited Slack fixture coroutine warnings.
- Unchanged installed lifetime plugin plus real HTTP, five real queue and twenty accepted/rejected busy/real-claim composition controls: **91 passed**.
- Supplied scope, queue and busy files alone: **43 passed** on each implementation.
- The release already supplies event binding, fold and debounce absorption. Its patch therefore touches only four native files; no redundant second absorption call remains.
- These are focused sets. Required exact-head CI, independent PASS and live same-session proof remain separate gates.
