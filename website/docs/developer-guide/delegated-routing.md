# Protected delegated routing

This opt-in gateway policy admits a Teams channel message only when its tenant,
stable parent channel and Entra person match a host-reviewed route. A route also
fixes the existing runtime profile, OAuth workload connection, HTTPS MCP endpoint
and exact remote read-tool bindings. It does not create a profile, enable
multiplexing, register a bot, sign in an account or change consent grants.

Absent configuration, or `enabled: false`, keeps legacy dispatch. When enabled,
each receiving bot named by a route is **exclusive** to its configured routes.
Unmatched people, tenants, channels, personal messages and unsupported activities
on that bot are refused. Other receiving bots and platforms retain legacy routing.
An omitted or null `bot_profile` selects the primary bot. The string `default`
and the active profile's name are rejected as selectors; a named value must select
an existing, served secondary profile with Teams enabled. The primary Teams adapter
must also be enabled when selected. Startup validates receiving bots as well as
destination profiles, before ingress or external secret hydration. Review this
blast radius before activation.

## Configuration

This example is deliberately disabled. Identifiers and tool names are synthetic;
replace them with reviewed host configuration. No endpoint or identity can be
selected through model arguments.

```yaml
gateway:
  multiplex_profiles: false  # Must already be explicitly enabled before activation.
  delegated_routing:
    enabled: false
    routes:
      - tenant_id: "11111111-1111-4111-8111-111111111111"
        channel_id: "19:example-channel@thread.tacv2"
        person_ids:
          - "22222222-2222-4222-8222-222222222222"
        client_ids:  # Required, nonempty operator-reviewed Entra azp allowlist.
          - "33333333-3333-4333-8333-333333333333"
        profile: "existing-profile"
        server: "reviewed-workload"
        url: "https://example.invalid/mcp"
        connection: "existing-workload-connection"
        tools:
          - name: "read_item"
            remote_name: "REPLACE_WITH_REVIEWED_REMOTE_READ_TOOL"
            description: "Read an item from the reviewed workload."
            input_schema:
              type: object
              properties:
                query: {type: string}
              required: [query]
              additionalProperties: false
```

A present policy must have an explicit Boolean `enabled`; empty objects and
null are configuration errors. A declared policy's YAML-load failure is fatal,
not a fallback to environment/default profile settings.

Enabled policy rejects malformed fields, ambiguous routes, missing client
allowlists, wildcard bindings, external schema references and unserved targets.
Session identity includes the profile, admitted tenant, person, channel and real
conversation. Reply addressing remains the original Teams conversation.

## Native authentication custody

The maintained Teams HTTP server validates the Bot Framework service JWT first.
A wrapper installed after SDK initialization admits activities before App
observers, attachment downloads or gateway hydration. Protected requests are
text-only; attachments are refused.

Channel OAuth is not used. The same bot creates a native personal conversation for
the authenticated sender and tenant, obtains the vendor sign-in resource and sends
the native OAuth card **only there**. Personal messages never run a profile, model,
memory or tool. A personal token exchange must match the server-created personal
conversation, Framework sender, Entra person, tenant, bot, connection and exchange
resource. The original channel question and reply reference remain in memory for
at most three minutes and resume at most once. Failure to create the personal
conversation is a denial, with no default-profile fallback.

The SDK Entra validator verifies the assertion signature, issuer, audience and
scope. Additional checks require v2 GUID bot audience, matching tenant/person,
reviewed `azp`, `access_as_user`, nonfuture `nbf`/`iat` and unexpired `exp`, without
the SDK's clock-skew allowance. A still-valid cached user assertion may complete a
fresh, bound personal challenge; its issue time is not the activity's freshness.
The workload token is opaque: its connection, channel and timezone-aware expiration
are checked without decoding it as identity proof. Cached workload tokens alone
cannot authorize a turn. There is no profile or CLI OAuth-cache fallback.

The private vault retains credentials only in server memory. Events carry an
opaque receipt, never a token or assertion. Source/content fingerprints, generation
checks and shared revocation survive task and thread context copies. Every token
service await is followed by a generation and original-occurrence check. A newer
question, stop, reset, session switch, cancellation, merge, debounce, steering or
redirect cannot extend old authority. `/stop`, `/new` and `/reset` are host controls;
other slash commands are refused in this lane. New questions need a new challenge.

At most 256 partitions may be authenticating or executing at once. Completed,
failed and expired occurrences release their entries; an in-flight exchange still
counts until its deadline. Expired entries are reaped before admitting another
request. Process-unique issuance generations prevent a late callback from reviving
an occurrence after its partition is freed and reused. The bounded duplicate cache
retains up to 4096 recent message IDs for ten minutes; a full cache denies new
questions until entries expire, rather than forgetting replay protection.

## Executor boundary

The normal gateway turn runner and agent are reused. Protected agent construction
skips profile memory and general context-file loading, while retaining the selected
profile's explicitly enabled static SOUL identity/charter. Review that identity
file before activation; it must not contain person-private workload data. It exposes only the reviewed tool
schemas. Registry, agent-inline and model-tool entry points intercept protected
calls before ordinary middleware, hooks and Tool Search. Terminal, file, browser,
code execution, delegation and message-send handlers are unreachable. Global MCP
tool/resource handlers also refuse protected contexts.

Reviewed aliases remain exact throughout sequential and concurrent agent-loop
dispatch; legacy aliases are not rewritten in protected turns. Protected success,
error and cancellation results bypass ordinary lifecycle pre/post hooks, progress
and completion callbacks, guardrail observers and file-verifier observers. The
normal per-person transcript remains available to the agent. Protected remote
payloads exceeding 50,000 serialized characters are rejected in full before
credential scanning or model/transcript publication, with a fixed narrow-read
error rather than partial data or general recovery-tool hints. Per-result and
model-scaled aggregate limits are applied in memory before incremental transcript
flush; aggregate finalization never enters general spill storage or steering
observers, even after the bound receipt is invalidated or lost. This is an output
publication bound, not an HTTP-response ingestion or remote-data-size guarantee.

The native final sender retains the bound occurrence throughout its awaits. Reply
work registers for cancellation on revocation or shutdown and has the grant's
expiry deadline. A native SDK HTTP interceptor rechecks authority after awaited
bot-token resolution, before transport handoff. This suppresses stale replies
still waiting before dispatch, including a sender that catches cancellation.
Once handed to the HTTP transport, delivery may already have occurred: cancellation
cannot retract that request or distinguish delivery from a lost response. There
is no automatic resend at this unknown-delivery boundary.

Each allowed call opens its own maintained MCP `ClientSession` and streamable HTTP
transport with only the bound person's bearer, then closes them and clears local
client header references. The HTTP request gate rechecks authority on every
request and denies redirects, retries/resumption requests, duplicate requests,
changed endpoint/tool/arguments and remote parameter-header mappings. There is no
fallback token or transparent resend after response loss. Sampling and elicitation
are not advertised and SDK defaults refuse them. Remote schemas do not introduce
header mappings or external validation fetches.

Exact bearer/assertion echoes and credential-shaped result fields are denied,
including JSON embedded in text. Protected MCP protocol logging is suppressed to
avoid recording raw remote data. This is a credential-custody boundary, **not a
broad data-loss-prevention system**. Allowed read results and the host-owned final
answer can contain workload data.

An exact local read allowlist does **not** make broad Microsoft `.All` grants
read-only. Operators must review each remote tool's real semantics, the connection's
consent grants and their residual write capability. Local revocation prevents
further requests and suppresses late results; it cannot undo an already issued read
or detect a server-side token revocation before the resource rejects it.

## Activation prerequisites and validation limits

Activation is a separate authorized operation. Review the receiving bot's exclusive
route scope, served profiles, exact endpoint/tool schemas, OAuth connection,
per-person tenant membership, approved client actors and grants first. The bot must
be installed and allowed to create the bound personal conversation. The personal
SSO flow must supply a sufficiently fresh bot-audience assertion.

The build was exercised with Teams Apps 2.0.13.4 / API 2.0.15, MCP 2.0.0, httpx2
2.7.0 and Python 3.11. The protected carrier explicitly requires MCP 2.x; it never
falls back to a different installed transport. This repository's current MCP and
Teams extras already pin the tested primary versions; no dependency installation
or pin changes are part of this feature.
The review corrections were also exercised against the release-locked Teams Apps,
API and Common packages all at 2.0.13.4, with MCP 2.0.0 and httpx2 2.7.0.

Synthetic signed-JWT tests exercise the real SDK HTTP route and personal API
primitives, normal gateway/agent setup, worker context propagation and maintained
MCP client using local HTTP fixture transports. They do not establish live tenant,
consent, WorkIQ endpoint, socket-listener or production readiness. A separate
operator-controlled review and live supported-flow check are required before use.

The personal-scope requirement follows the supplied Microsoft references:
[bot SSO scope](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/bot-sso-overview)
and [adding authentication](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/add-authentication).
