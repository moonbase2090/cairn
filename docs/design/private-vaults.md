# Private vaults

Status: spike proposal for review. This document records repository findings,
loopback experiments, and a proposed design. It changes no runtime behavior.

## Decision and language

Cairn has no private memories inside a vault. A vault is the access boundary:
every agent allowed into it shares all of its contents. Secrets never belong in
any vault.

Use separate vaults only when the audience differs, such as personal versus
work information, different clients, or unreleased security work. Roles within
one project share one vault. A task or team label is like a folder an agent may
choose to look in; it helps focus and rank results, but it does not lock them
away. The vault is the locked room.

Personal, customer, client, and unreleased-security details belong only in a
vault whose access list includes the agents that need them. Do not create
planner-only or researcher-only vaults inside the same project.

## Current behavior

| Question | Finding | Evidence |
|---|---|---|
| How does CLI choose a vault? | Each CLI invocation creates one client. `--vault` wins, then `CAIRN_DIR`, then `./.cairn`. | [`vault_dir`](../../src/cairn/cli.py#L59), [`build_client`](../../src/cairn/cli.py#L223), parser option [`--vault`](../../src/cairn/cli.py#L237) |
| How does bootstrap choose a vault? | Bootstrap writes one absolute `CAIRN_DIR` and one `CAIRN_AGENT` into the project MCP config. It currently uses the fixed server key `cairn`, so running bootstrap again for another vault replaces that entry. | [`_cmd_bootstrap`](../../src/cairn/cli.py#L964), MCP config write at [`cli.py:987`](../../src/cairn/cli.py#L987) |
| Can one MCP process use several vaults? | No. `make_client()` reads one `CAIRN_DIR` (or the current directory), opens one backend, and passes one `CairnClient` to every tool call. A tool call has no vault selector. | [`make_client`](../../src/cairn/mcp_server.py#L108), [`handle`](../../src/cairn/mcp_server.py#L519) |
| Can an agent use several vaults? | Yes, by connecting to separately configured MCP server processes, each pinned to its own vault. CLI commands can likewise specify `--vault`. Search and exact lookups run on that client’s backend only. | [`_tool_retrieve`](../../src/cairn/mcp_server.py#L143), [`_tool_get`](../../src/cairn/mcp_server.py#L171) |
| Does MCP make the active vault obvious? | Not reliably today. CLI `whoami` reports the effective vault path, but the MCP `cairn_whoami` response reports agent, embedder, dimensions, and count without a vault name or ID. | [`_cmd_whoami`](../../src/cairn/cli.py#L882), [`_tool_whoami`](../../src/cairn/mcp_server.py#L188) |
| What protects a local MCP vault? | Stdio MCP uses the configured filesystem path. `CAIRN_DIR` selects storage; it is not an authorization token. Local isolation depends on who can access the directory and which MCP entries the host exposes. | [`make_client`](../../src/cairn/mcp_server.py#L108), bootstrap environment at [`cli.py:987`](../../src/cairn/cli.py#L987) |
| Can one `cairn serve` host multiple vaults? | No. The HTTP server’s client factory closes over one client and reopens that client’s backend for each request. Per-agent tokens are looked up in that backend’s token table. | [`_factory_for`](../../src/cairn/serve.py#L29), [`_authorize`](../../src/cairn/serve.py#L62), [`_bind`](../../src/cairn/serve.py#L306) |
| What does a token currently authorize? | A per-agent token authenticates against one server’s token registry. It gives that agent shared-vault reads and writes to its own rows; the curator flag permits cross-agent state changes and correction resolution. It is not a private row or task role. | [`_authorize`](../../src/cairn/serve.py#L62), [`_sync_pack_matches_agent`](../../src/cairn/serve.py#L84), [sync ownership policy](sync-updates.md#ownership-and-curator-tokens) |
| Where do sync cursors and conflicts live? | SQLite and PostgreSQL store tokens, event feeds, tombstones, cursors, and conflict records alongside a vault’s memories. Server cursor rows include peer and direction; HTTP sync additionally incorporates token ID and peer identity. | [SQLite schema](../../src/cairn/store.py#L70), [token backend contract](../../src/cairn/storage.py#L174), [cursor and conflict contract](../../src/cairn/storage.py#L212), [`/pull` cursor scope](../../src/cairn/serve.py#L205), [`/push` cursor scope](../../src/cairn/serve.py#L272) |

Task and team filters narrow a search request, but are supplied by the caller
and are not authorization checks. They must never be described as privacy
controls.

## Loopback experiments

These checks used temporary SQLite vaults and loopback-only processes; all
temporary directories and server threads were removed afterward.

1. I started two stdio MCP subprocesses with the same `CAIRN_AGENT` and
   different `CAIRN_DIR` values. Each process stored and retrieved its own
   unique marker; neither returned the other vault’s marker. This confirms
   multiple-vault use requires distinct MCP process configurations today.
   `cairn_whoami` reported the same agent and one memory in each process, but
   did not identify the vault. This confirms isolation between independently
   configured processes; the loopback check did not test a host UI connecting
   to both at once.
2. I started two loopback `cairn serve` instances in per-agent-token mode, each
   over a separate vault with a separate token registry. Each valid token
   pulled only its vault’s marker. Using token A against server B and token B
   against server A returned HTTP 401.

The experiments validate current process-level isolation. They do not test
cloud IAM policies or claim that secret scanning can identify every secret.

## Options

### Option A: one process and endpoint per vault

Run one MCP process and, when needed, one sync server per vault. Each process
opens one backend, has one token registry, and owns one set of event feeds,
cursors, tombstones, and conflicts. Give every MCP server a descriptive host
name such as `cairn-team-acme` or `cairn-personal`; do not hide the choice in a
mutable “current vault” setting.

This matches the current `CairnClient` ownership model and has the smallest
authorization change. The main product work is to let bootstrap register
multiple named MCP servers without overwriting an existing one, and to report
the selected vault identity in MCP. Each remote vault gets its own endpoint,
port or URL, credentials, and lifecycle state.

### Option B: multiplex several vaults in one process

Add a vault registry and require every tool and HTTP request to select a vault.
Bind each token to an explicit set of vault IDs and capabilities. Route reads,
writes, sync events, conflict resolution, and search through the authenticated
membership rather than trusting a caller-supplied vault ID.

This uses fewer processes and endpoints, but it changes the ownership boundary
of MCP tools, HTTP handlers, cloud adapters, cursor state, and caches. Any
missed route or vector filter can expose another audience’s data. It also
creates a less obvious agent choice unless every operation names its vault.

### Recommendation

Start with Option A. It matches how the current clients and servers are
constructed, and the loopback checks demonstrate separate token registries and
read results. Add a stable logical `vault_id` to vault metadata and sync
handshakes, so replicas of one shared vault can identify one another and a
misconfigured peer cannot silently mix audiences. Keep per-replica sync
`origin_id` separate from this logical vault ID.

Do not create a new audience boundary by copying a live vault directory or
cloud namespace as-is: that also copies its ID and may copy active server token
grants. A new-audience fork must receive a new `vault_id`, start with no active
grants, and reset peer cursors. Replicas of the same audience intentionally
preserve the ID. A replica created by copying a live directory must clear the
copied server-token rows and peer cursors before serving at a new endpoint, then
receive its own grants. Otherwise the copied bearer token works at both
endpoints and revocation must happen at each one. Sync-created replicas do not
receive token rows because grants are not part of sync packs. Restoring a backup
to replace the same endpoint is recovery, so it preserves that endpoint’s
identity, grants, and cursors.

Do not add multi-vault routing until a concrete host cannot expose multiple
named MCP servers. If multiplexing is later needed, require explicit vault
selection on every operation and enforce membership before touching any
backend.

## Access grants and revocation

Treat membership as a grant to the entire vault. A member may read all rows and
may create or change rows attributed to its assigned `agent_id`. A curator may
also change another agent’s state and resolve cross-agent competing
corrections. Task IDs and team IDs do not change these rights. The existing
curator permission is a write privilege within a shared audience, not a
separate vault role.

For remote sync, keep one token registry per vault, as today. Local stdio MCP
has no token gate; use filesystem permissions and give each agent host only the
MCP entries for vaults that agent may use. Do not rely on an agent merely
choosing the right name if the host exposes a more restricted vault to it.
Define each remote grant with:

- a random public `token_id` and a one-time-displayed bearer token;
- a digest of the bearer token, never the bearer value;
- the stable `vault_id`, assigned `agent_id`, and member/curator role;
- `created_at`, optional expiry, and revocation metadata;
- optional human-readable recipient and grant note, containing no secrets.

The vault administrator creates and revokes grants with `cairn_token`. Revoking
a token makes its next request fail. Current sync event history and cursor rows
can outlive a deleted token row because peer identity includes the token ID;
that is acceptable for v1, but document retention and cleanup separately.
Retaining a grant revocation record would improve audit history. Keep grant
management local to a trusted vault administrator. A remote member token must
not be able to mint or revoke other members. Add a read-only capability only if
a real use case needs it; the current token model intentionally gives an agent
shared read access and agent-owned writes.

For a future multiplexed server or cloud endpoint, the registry key and
authorization lookup must include `vault_id`. An authenticated token’s allowed
vault membership must be checked before resolving a backend, executing search,
reading content, or returning a cursor. The ID in a request is a selector, not
proof of access.

## Agent choice and safe defaults

Keep the project’s bootstrapped vault as the default for that project. Add
separate, named MCP entries for each additional audience boundary. An agent
must not search across configured vaults unless it explicitly calls tools on
each named server; Cairn must never aggregate results implicitly.

Only configure an agent’s host with named entries for its approved vaults. On a
single-user machine, the operator who controls the Cairn directory and MCP
configuration controls local membership. For remote or cloud access, enforce
membership with the token registry or provider IAM policy; a vault label alone
does not grant or restrict access.

At session start, `cairn_whoami` should report the logical vault name and
stable ID, agent ID, storage type, and whether this token is a member or
curator. Use a safe human-readable vault label rather than exposing an absolute
filesystem path by default. Include the selected vault name/ID in store,
retrieve, and list tool results, and put the audience in each MCP server name.
If identity is missing or the tool connection cannot establish its vault,
fail closed rather than falling back to another vault.

Bootstrap should add or update only the named Cairn MCP entry. It must preserve
other named entries and detect a name collision that points at a different
vault. The skill should tell the agent to choose the server whose audience
matches the data, call `cairn_whoami`, and keep each query within that one
connection.

Suggested skill text:

> A vault is the locked room: everyone with access can read everything inside
> it. Task and team labels are like folders any agent can choose to look in;
> they help focus and rank results, but they do not grant or restrict access.
> Keep personal, customer, client, or unreleased-security details only in a
> vault available to the agents who need them. Never store secrets, credentials,
> access tokens, or private keys in any vault.

Place this in both the onboarding tutorial and installed skill. Replace the
current tutorial instruction to “use a private task scope for sensitive
notes”; keep the existing “no credentials” warning in the skill and expand it
with the vault boundary language. The current text is at
[`tutorial.py:41`](../../src/cairn/tutorial.py#L41) and
[`agent_skill.md:403`](../../src/cairn/agent_skill.md#L403).

## Secret blocking on content ingress

Implement one offline scanner at content entry, before content hashing,
embedding, audit output, document creation, or persistence. `store_memory()` is
the main agent entry point. Document ingestion chunks and batch-embeds content
before calling `store_memory()`, so scan those chunks before batch embedding as
well. Portable imports and `cairn-sync-2` snapshots write directly to storage,
so validate every incoming content before applying any row or advancing a
cursor. Scanning only `store_memory()` would still allow a secret-bearing pack
to enter a vault. Relevant paths are [`CairnClient.store_memory`](../../src/cairn/client.py#L98),
whose two direct insert sites are [`client.py:132`](../../src/cairn/client.py#L132)
and [`client.py:165`](../../src/cairn/client.py#L165); [`import_pack`](../../src/cairn/client.py#L347)
inserts snapshots at [`client.py:373`](../../src/cairn/client.py#L373);
`import_sync_pack` applies events at [`store.py:911`](../../src/cairn/store.py#L911)
and [`postgres.py:607`](../../src/cairn/postgres.py#L607). Document ingestion
scans each chunk before batch embedding at [`ingest.py:91-92`](../../src/cairn/ingest.py#L91),
then calls `store_memory()` at [`ingest.py:104`](../../src/cairn/ingest.py#L104).
Apply the same contract to
future cloud writes.

Start with three detector families:

1. **Private-key blocks.** Reject recognized private-key PEM labels, including
   PKCS #8 `PRIVATE KEY` and `ENCRYPTED PRIVATE KEY`, RSA and EC labels, and
   OpenSSH private-key blocks. Handle both literal newlines and escaped `\\n`
   representations. RFC 7468 defines the textual `PRIVATE KEY` encoding and
   gives the standard BEGIN/END framing.
2. **Documented provider formats.** Match GitHub’s current token prefixes
   (`ghp_`, `github_pat_`, `gho_`, `ghu_`, `ghs_`, `ghr_`) without assuming one
   fixed token length; GitHub is rolling out a new stateless `ghs_` format.
   Reject AWS access-key IDs beginning `AKIA` or `ASIA` when they occur as
   credential material, and detect nearby `aws_secret_access_key` or
   `aws_session_token` assignments. AWS documents those access-key ID
   categories and recommends against embedding long-term access keys in code.
3. **Generic credentials and entropy.** Inspect likely assignment contexts
   (`password`, `secret`, `api_key`, `token`, `authorization: Bearer`) and
   opaque candidate spans. Use minimum length, alphabet-aware Shannon entropy,
   and boundaries so normal prose and common identifiers are not scanned as a
   single secret. Treat the base64/hex thresholds in `detect-secrets` as
   starting points for fixture-driven tuning, not a guarantee.

GitHub’s supported-pattern list is maintained and changes over time, so keep
the initial detector set intentionally small and add reviewed formats through
versioned rules and tests. Do not make a network verification call with memory
content. Gitleaks publishes its detector rules under MIT and Yelp’s
`detect-secrets` under Apache-2.0; use their rule sets as references, and
review license and attribution obligations before copying implementation or
large rule fragments.

On a match, reject with a short message such as `secret-like private key
detected; remove credentials before storing this memory`. Return the finding
category only. Never include the matched value, surrounding content, a content
hash, or a candidate excerpt in the exception, MCP response, audit record,
server log, or telemetry. Do not provide a silent allowlist or override in the
first version; a false positive should be addressed by removing the opaque
value or by a reviewed detector change with a regression fixture.

Detection is a guard against common accidental disclosures, not proof that a
memory is secret-free. Arbitrary passwords and novel credentials may not match
these rules. Keep the product instruction absolute (“never store secrets”),
and test common patterns, escaped key blocks, false-positive prose, and the
fact that rejected content is absent from every storage and logging path.

Do not rewrite existing memories during a schema migration. Existing vaults
may already contain content that the scanner recognizes, and SQLite/PostgreSQL
sync migrations can backfill old rows as snapshot events ([SQLite](../../src/cairn/store.py#L530),
[PostgreSQL](../../src/cairn/postgres.py#L439)). Add a read-only preflight scan
that reports finding categories without showing matched content or memory
keys; run it before enabling a new scanner rollout or syncing an existing
vault. Remediation and secret rotation stay explicit operator actions. This
preflight does not change the no-override rule for new writes.

### Detection references

- [GitHub token formats](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/about-authentication-to-github) documents current credential prefixes and token-format changes.
- [AWS programmatic access credentials](https://docs.aws.amazon.com/IAM/latest/UserGuide/security-creds-programmatic-access.html) documents `AKIA` and `ASIA` access-key IDs and safer alternatives to long-term keys.
- [RFC 7468](https://www.rfc-editor.org/rfc/rfc7468.html) defines textual private-key labels and framing.
- [GitHub supported secret-scanning patterns](https://docs.github.com/en/code-security/reference/secret-security/supported-secret-scanning-patterns) shows the breadth and changing nature of provider-specific patterns.
- [Gitleaks rules](https://github.com/gitleaks/gitleaks/blob/master/cmd/generate/config/rules/github.go) and its [MIT license](https://github.com/gitleaks/gitleaks/blob/master/LICENSE) provide a reference for provider patterns.
- [Yelp detect-secrets](https://github.com/Yelp/detect-secrets) documents regex, keyword, and entropy-based detectors; its [Apache-2.0 license](https://github.com/Yelp/detect-secrets/blob/master/LICENSE) applies to reuse.

## Sync and cloud isolation

### Local sync

Keep each logical vault’s memories, event feed, tombstones, competing-correction
records, token grants, and peer cursors in that vault’s backend. Option A gives
each running sync server a backend and token registry for only one vault. Two
agents may sync to the same endpoint only when both are granted membership in
that vault. A token for one endpoint conveys no authority at another endpoint.

Add a stable logical `vault_id` to the sync handshake and persist it in vault
metadata. Replicas that intentionally share one audience carry the same ID;
separate audiences get different IDs. Keep `origin_id` as the per-replica event
writer identity. Reject a sync pack whose logical vault ID does not match the
configured peer at the start of `import_sync_pack()`, before examining or
applying any event, writing tombstones, or advancing a cursor. Do this even when
embedder and dimensions match; otherwise an event-only tombstone could pollute
the receiving vault before the mismatch is noticed. Keep cursors local to
`(vault_id, peer, token_id, direction)`. The HTTP handshake returns the peer's
`origin_id`; each replica keys its cursor by the other replica's origin ID. Use
the same `shared` token sentinel on both sides, and read the legacy empty-token
cursor when upgrading a shared-token server. A client with an older URL-keyed
cursor safely replays from zero; event IDs make that replay idempotent. Separate
local databases provide the vault boundary today, and the explicit key is
needed by cloud tables or any later shared server.

Curator rights remain local to one vault. Resolving a competing correction
changes ordinary state events in that vault and its replicas. Do not let a
curator token from one audience resolve a conflict in another vault.

### Cloud backends

Extend the existing [cloud backend proposal](cloud-backends.md) rather than
adding a second provider abstraction. Its shared `StorageBackend`, provider
control plane, sync transport, AWS CDK implementation, and Pulumi TypeScript
designs for Google Cloud and Azure remain the intended structure.

For the AWS design in [`s3-dynamodb-backend.md`](s3-dynamodb-backend.md), scope
every resource by stable `vault_id`:

- DynamoDB memory rows, token grants, change events, tombstones, cursors, and
  conflict records use a vault-qualified partition key. Transactions must
  keep a row update and its event inside that same vault partition where the
  data model permits it.
- S3 content object keys include the vault ID, with a per-vault KMS key or
  tightly scoped KMS grants. Never infer authorization from an object key
  supplied by the agent.
- Give each vault its own S3 Vectors index in the first AWS version. A shared
  index with a metadata filter is cheaper in resource count but makes every
  query depend on the filter being applied correctly. If later consolidated,
  include `vault_id` in every vector record and query filter, then verify each
  candidate against the authoritative DynamoDB row before returning content.
- Scope direct AWS IAM grants to one vault’s tables, object prefix, vector
  index, and key. For clients without direct AWS rights, the Lambda/API Gateway
  endpoint authenticates a digest-backed token, checks its vault membership,
  then routes `push`, `pull`, health, or search to that vault. The endpoint may
  accept a vault ID as a path component, but the token grant must independently
  authorize that ID.

For the later Google Cloud and Azure adapters, apply the same logical boundary:
Firestore or Cosmos metadata and token records carry the vault ID; object
storage paths include it; vector search is separated by index or mandatory
vault filter followed by authoritative row verification; and Cloud Run or
Azure Functions checks the token’s vault membership before serving sync or
search. Provider-specific cursor encodings remain opaque, but the stored
cursor key always includes the vault identity.

The local `origin_id`, task labels, team labels, provider project, bucket name,
or database partition alone are not authorization decisions. The token or
provider principal must be checked against the requested vault before data is
read or changed.

## Implementation outline for a later decision

1. Add immutable logical `vault_id` and display name to initialized vault
   metadata; include the ID in `cairn_whoami`, MCP tool responses, and sync
   pack negotiation. Preserve existing vaults with a one-time migration.
2. Define replica versus new-audience fork behavior. A fork gets a new ID,
   drops copied grants, and clears peer cursors. A newly provisioned replica
   preserves the ID but clears copied endpoint grants and cursors; a same-
   endpoint backup restore preserves them.
3. Make bootstrap additive and nameable for extra audience-specific vaults.
   Detect MCP-name collisions and print each configured audience. Keep the
   project vault as the only implicit default.
4. Document the grant model and update MCP/CLI token create, list, and revoke
   output. Add explicit expiry/revocation metadata only with migration and
   audit behavior defined.
5. Implement the offline secret admission scanner on stores, portable imports,
   sync snapshot imports, and document chunks before batch embedding or
   persistence. Add a content-free read-only preflight for existing vaults;
   do not auto-redact or rewrite historical rows.
6. Add sync identity validation and regression coverage for matching replica
   IDs, mismatched IDs, cursors per peer/token/direction, and conflict
   resolution staying within one vault.
7. Add isolated per-vault cloud authorization and resource naming to the AWS
   adapter first, then the shared GCP/Azure provider contract. Cover direct IAM
   and Lambda/API Gateway token paths independently.

This list is a design outline only. No feature implementation or PR is part of
this spike.
