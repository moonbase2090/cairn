# cairn

Local-first shared memory for CLI agents — no accounts, no keys, no cloud.
One SQLite file, no API keys, no server. The agent *is* the LLM; `cairn` is the memory.

## Quickstart

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
export CAIRN_DIR=./.cairn CAIRN_AGENT=claude-myproj
cairn init         # prompts for project/agent/team (or --yes for defaults; refuses without an identity)
cairn bootstrap    # .mcp.json + AGENTS.md + onboarding pack — agents self-onboard from here
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
| `export` / `import` | git-native sync: idempotent JSON packs, commit them, merge by key-union |
| `serve` / `push <url>` / `pull <url>` | HTTP team sync with bearer token (`--token` or `$CAIRN_TOKEN`) |
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

## Team mode (two transports, same merge)

```bash
# git-native (no server): export packs, commit, teammates import
cairn export --out memory/q2.jsonl && git commit -m "memory: q2" memory/q2.jsonl
cairn import memory/q2.jsonl   # union by key — never conflicts

# server (high churn): one peer serves, others push/pull
cairn serve --port 8778                      # prints its token
cairn push http://peer:8778 --token $T
cairn pull http://peer:8778 --token $T
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
`cairn-mcp`: six verbs + howto + ops parity (`cairn_whoami`, `cairn_gc`,
`cairn_export`, `cairn_import`, `cairn_ingest`), stdlib-only JSON-RPC.
Only `cairn serve` stays CLI-only.

## Embedders (pluggable, always keyless by default)

| Embedder | Setup | Dims | Related-pair sim | Notes |
|---|---|---|---|---|
| `hash` (default) | nothing | 384 | 0.33 | offline, zero downloads. Coarse — fine for tests/demos |
| `fastembed` | `pip install -e ".[embed]"`, `init --embed-spec fastembed` | 384 | **0.80** | local ONNX BGE-small. CLI/MCP share one process via `cairn-embedd` (idle-exit 15m, `CAIRN_EMBEDD=0` forces in-process) |
| `ollama[:model]` | `ollama pull mxbai-embed-large`, `--embed ollama` | 1024 | **0.75** | best separation (unrelated pairs score 0.31 vs 0.42/0.0) |

Measured 2026-09-18 on `Q2 revenue grew…` / `how did Q2 revenue do?`; near-dup
`fox/dog→dogs` scores 0.97 (fastembed, trips the 0.95 screen) vs 0.92 (ollama —
no false block, explicit `--supersedes` still works). Thresholds are
embedder-sensitive by nature; 0.95 stays the default. Pinned in
`tests/test_real_embedders.py` (auto-skips when a backend is absent).

Each vault is tagged `embed_model+dims` at init; cross-space open/import is refused.

## Layout

```
src/cairn/models.py  # records, deterministic keys, store results
src/cairn/embed.py   # Embedder protocol + hash/fastembed/ollama + SocketEmbedder
src/cairn/embedd.py  # cairn-embedd: one ONNX session, Unix socket
src/cairn/store.py   # vault.db: metadata + sqlite-vec index (brute-force fallback)
src/cairn/client.py  # six verbs + gc + export/import + stats
src/cairn/serve.py   # HTTP team sync (stdlib only, per-request connections)
src/cairn/ingest.py  # docs seeding (section chunks, idempotent)
src/cairn/galaxy.py  # HTML starfield (numpy PCA) + local HTTP host
src/cairn/cli.py     # `cairn` binary (human + position-independent --json)
```

## Hybrid storage (schema 2)

`.cairn/` is a vault *directory*, not just a DB:

```
.cairn/vault.db        # metadata + embeddings + BM25 index (small, fast)
.cairn/docs/ab/cd/<hex>.md  # full text of big memories, content-addressed
```

Memories over `doc_threshold` (default 2048 bytes, `cairn init --doc-threshold`)
spill to `docs/`; everything else reads identically. Same-content versions share
one file (refcounted — deleted with its last row, plus `gc` sweeps strays).
Packs carry full content, so `export`/`import` and git-sync are unchanged.
Doc files are hash-verified on read; corruption raises loudly, never silently.
Old vaults migrate on open (rowids preserved) after a `vault.db.pre2.bak` backup.

## License

MIT — see [LICENSE](LICENSE).

