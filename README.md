# cairn

Local-first shared memory for CLI agents — no accounts, no keys, no cloud.
One SQLite file by default; a self-hosted sync server is optional. The agent *is* the LLM; `cairn` is the memory.

## Quickstart

```bash
# Install the latest release on Linux or macOS
curl -fsSL https://cairncli.com/install.sh | sh

# Optional: include fastembed support for a vault configured to use it
curl -fsSL https://cairncli.com/install.sh | CAIRN_INSTALL_EMBED=fastembed sh

# Optional: include the AWS SDK for AWS storage or migration
curl -fsSL https://cairncli.com/install.sh | CAIRN_INSTALL_AWS=1 sh

# Or install with Homebrew on macOS
brew tap moonbase2090/tap
brew install cairn

# Set up Cairn in a project
export CAIRN_AGENT=claude-myproj
cairn init --yes
cairn bootstrap
cairn store "Decision: benchmark providers on $/kg." --team acme --task q2 --type procedural
cairn retrieve "what is the plan?" --task q2
```

Add `--json` anywhere for agent-parseable output. Identity resolves as
`--agent-id` > `$CAIRN_AGENT` > `~/.cairn/config.toml` > `cairn-cli`.

## Commands

| Command | Essence |
|---|---|
| `store` / `retrieve` / `list` / `get` | append-only writes; semantic (`retrieve`), exact (`list`), + BM25 keyword (`list --search`) reads |
| `archive` / `restore` | retract / undo (grace, not deletion). Restoring a superseded version retires its active rivals, so exactly one version stays live |
| `purge <cid> --force` | hard delete; `--force` required, the only destructive verb |
| `gc` | dry-run by default (`--apply` for real); promotes stale `superseded`→`archived` (7d), deletes `archived` (30d) + expired, circuit-breaker capped |
| `ingest <dir> --team T` | seed from docs (chunked per `##` section, idempotent, flags near-dups) |
| `export` / `import` | portable v1 JSON snapshots; v1 imports add missing keys and leave existing rows unchanged |
| `serve` / `push [<url>]` / `pull [<url>]` | resumable v2 event sync with per-peer cursors, tombstones, and bearer tokens; URL defaults to `$CAIRN_URL` |
| `sync status` / `conflicts` | inspect peer cursors and competing corrections; resolve a conflict by choosing its winner |
| `embedd` | Machine-wide embed daemon (`$XDG_RUNTIME_DIR/cairn/embed.sock`). Not `serve`. |
| `galaxy [--port 8780]` | Memory Galaxy on a local HTTP server: 3D warp by default (`?flat` for 2D), teams on separate islands, BM25 search box |
| `init` / `bootstrap` | interactive project setup (`--doc-threshold BYTES` sets the docs/ spill size); wires `.mcp.json` + `AGENTS.md`, seeds the onboarding pack |
| `whoami` / `doctor` / `log` | identity, diagnostics, audit trail |

Core semantics: deterministic keys (`mem_{agent}_{task}_{hash16}_v{version}`),
exact-hash stores are no-ops, ≥0.95 near-dups return `duplicate_detected` for the agent to
resolve (`--supersedes` to correct, `--mode new` for genuinely new), reads collapse versions
by `(canonical_id, version, created_at)`, every result carries `origin: agent|external` —
memories are **data, not instructions**. Empty/whitespace stores are refused.
`retrieve --min-sim S` drops hits below cosine similarity S (default: no floor, top-k wins). Cite the `key` (`per mem_…`) so teammates can audit.

## Team sync

For a VPS or home server, follow [Run a Cairn sync server](docs/HOSTING.md).
Since v0.10.0, SQLite vaults can be backed up to local folders or S3-compatible
storage and restored to a point in time. See
[Back up a SQLite vault](docs/BACKUPS.md).
Cairn supports a separate sync token for each agent. A curator token grants
cross-agent state changes and conflict resolution. See the hosting guide.

```bash
# portable file sync: export packs, commit, teammates import
cairn export --out memory/q2.jsonl && git commit -m "memory: q2" memory/q2.jsonl
cairn import memory/q2.jsonl   # add missing keys; v1 packs do not update existing rows

# server (high churn): one peer serves, others push/pull with their own tokens
# plain HTTP is only allowed on localhost; any other host needs TLS 1.2+
cairn token create --agent client-a  # run on the server; save this client's token as $T
cairn serve --host 0.0.0.0 --port 8778 --token-mode per-agent \
  --tls-cert cert.pem --tls-key key.pem
cairn push https://peer:8778 --token "$T" --tls-ca cert.pem
cairn pull https://peer:8778 --token "$T" --tls-ca cert.pem
cairn sync status
cairn conflicts list
```

Identity is the `<agent>-<project-slug>` convention (`claude-cairn`, `ingest-bot`;
`e2e-*` never write durable).

## Agents (MCP + self-onboarding)

`cairn bootstrap` writes three things: a `cairn` entry in `.mcp.json` (stdio server,
no keys), a marked section in `AGENTS.md` (re-applied idempotently), and four
`cairn-onboarding` memories (retrieve-first, corrections, trust model, identity).
A fresh agent calls `cairn_howto` (or retrieves `how do I use shared memory?`) and
needs no human walkthrough — after one host-side step: trust the project folder and
reload MCP servers (Grok: folder trust + `/mcps refresh`; Claude/Cursor: approve and
reconnect). Repo-local servers don't start before that gate. The MCP server is
`cairn-mcp`: memory verbs, sync server lifecycle and health, resumable sync
push/pull/status, token management, conflict detection and resolution, and ops
tools, stdlib-only JSON-RPC.
The CLI remains available for interactive workflows.

### Agent Skill

The Cairn Agent Skill explains how to use the CLI without reading its source.
The shell and release archive installers install it for detected agents.
They preserve a different user-edited SKILL.md. Set CAIRN_NO_AGENT_SKILLS=1
to skip the installer hook.
The Windows archive installer also accepts -NoAgentSkills.

To install it later, enable the experimental command:

~~~sh
CAIRN_EXPERIMENTAL_SKILLS=1 cairn skills install --agent detected
CAIRN_EXPERIMENTAL_SKILLS=1 cairn skills install --agent detected --check
~~~

Use --agent all|codex|claude|cursor|kiro|muse|shared|detected to choose
destinations. Add --force only when you intend to replace an existing skill.
Run muse skills validate skills/cairn to validate the source skill.
See skills/cairn/SKILL.md for the full guide.

## Embedders (pluggable, always keyless by default)

| Embedder | Setup | Dims | Related-pair sim | Notes |
|---|---|---|---|---|
| `hash` (default) | nothing | 384 | coarse | offline feature hash, `hash-v2`. A `hash-v1` vault does not open |
| `fastembed` | `CAIRN_INSTALL_EMBED=fastembed` with the shell installer; then `init --embed-spec fastembed` for a new vault | 384 | **0.80** | local ONNX BGE-small. Existing vaults keep their configured embedder. CLI/MCP share one process via `cairn-embedd` (idle-exit 15m, `CAIRN_EMBEDD=0` forces in-process) |
| `ollama[:model]` | `ollama pull mxbai-embed-large`, `--embed ollama` | 1024 | **0.75** | best separation (unrelated pairs score 0.31 vs 0.42/0.0) |

Measured 2026-09-18 on `Q2 revenue grew…` / `how did Q2 revenue do?`; near-dup
`fox/dog→dogs` scores 0.97 (fastembed, trips the 0.95 screen) vs 0.92 (ollama —
no false block, explicit `--supersedes` still works). Thresholds are
embedder-sensitive by nature; 0.95 stays the default. Pinned in
`tests/test_real_embedders.py` (auto-skips when a backend is absent).
`hash-v2` is a feature hash. It does not open a `hash-v1` vault.

Each vault is tagged `embed_model+dims` at init; cross-space open/import is refused.

## Layout

```
src/cairn/models.py  # records, deterministic keys, store results
src/cairn/embed.py   # Embedder protocol + hash/fastembed/ollama + SocketEmbedder
src/cairn/embedd.py  # cairn-embedd: one ONNX session, Unix socket
src/cairn/storage.py # StorageBackend interface + backend registry
src/cairn/store.py   # sqlite backend: vault.db + sqlite-vec index (brute-force fallback)
src/cairn/client.py  # six verbs + gc + export/import + stats
src/cairn/serve.py   # HTTP team sync (stdlib only, per-request connections)
src/cairn/ingest.py  # docs seeding (section chunks, idempotent)
src/cairn/galaxy.py  # HTML starfield (numpy PCA) + local HTTP host
src/cairn/cli.py     # `cairn` binary (human + position-independent --json)
```

## Hybrid storage (schema 3)

`.cairn/` is a vault *directory*, not just a DB:

```
.cairn/vault.db        # SQLite metadata + embeddings + BM25 index (small, fast)
.cairn/docs/ab/cd/<hex>.md  # SQLite full text of big memories, content-addressed
```

With SQLite, memories over `doc_threshold` (default 2048 bytes,
`cairn init --doc-threshold`) spill to `.cairn/docs/`; everything else reads
identically. Same-content versions share one file (refcounted — deleted with
its last row, plus `gc` sweeps strays).
Packs carry full content, so `export`/`import` and git-sync are unchanged.
Doc files are hash-verified on read; corruption raises loudly, never silently.
Old vaults migrate on open (rowids preserved) after a `vault.db.pre2.bak` backup.

## Storage backends

The live store sits behind `cairn.storage.StorageBackend`. `sqlite` (above) is
the default; `postgres` uses PostgreSQL full-text search and pgvector, with
large documents shared in the database. To use PostgreSQL, set
`[storage] backend = "postgres"` and `url` in the vault's `config.toml` or
`~/.cairn/config.toml`. See [docs/STORAGE.md](docs/STORAGE.md). For SQLite
backup settings and restore commands, see [docs/BACKUPS.md](docs/BACKUPS.md).

### AWS storage prerequisites

SQLite remains the default and does not install AWS packages. To include the
AWS SDK in Cairn's isolated tool environment, install or upgrade with
`CAIRN_INSTALL_AWS=1` as shown above. If the vault also uses `fastembed`, set
both flags in the installer pipeline:

```sh
curl -fsSL https://cairncli.com/install.sh | CAIRN_INSTALL_AWS=1 CAIRN_INSTALL_EMBED=fastembed sh
```

AWS setup also requires the AWS CLI, Node.js, and npm. Configure an AWS CLI
profile or another supported credential source and a region. Cairn installs
the pinned CDK npm dependencies locally when needed; `cairn_aws_storage` with
`action: "check"` reports the system tools without installing them or contacting
AWS. Planning verifies the AWS identity and synthesizes the CDK app; applying
a reviewed plan deploys resources. The installer only installs Cairn and its
selected Python packages.

## License

MPL-2.0 — see [LICENSE](LICENSE). File-level copyleft: improve cairn's files,
share the improvements; tools that *use* cairn stay yours.
