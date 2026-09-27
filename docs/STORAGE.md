# Storage backends

A vault has two parts:

- **The vault directory** (`.cairn/` or `$CAIRN_DIR`) on local disk. It holds
  `project.json` (project, team, agent identity), the `embedder` hint,
  `audit.jsonl`, and an optional `config.toml`.
- **The live store**: memories, keyword search, vector search, and large
  memory documents. It sits behind `cairn.storage.StorageBackend`.

`sqlite` is the default and, for now, the only backend. It keeps everything in
the vault directory: `vault.db` (tables, FTS5 index, sqlite-vec index) and
`docs/` for memories larger than the document threshold.

## Choosing a backend

Set `[storage] backend` in a `config.toml`:

```toml
[storage]
backend = "sqlite"
```

cairn reads the first of these that sets it:

1. `<vault dir>/config.toml`, for this vault only
2. `~/.cairn/config.toml`, for every vault on the machine
3. the default, `sqlite`

`cairn init`, every CLI command, and `cairn-mcp` use the same lookup. An
unknown name fails before anything is created or opened:

```
error: unknown storage backend 'postgres'; available: sqlite
```

A `config.toml` that cannot be parsed, or whose `[storage]` section is
malformed, is also an error rather than a silent fallback to SQLite.
`cairn doctor` reports the backend in use as `storage`.

## The interface

`StorageBackend` (in `src/cairn/storage.py`) covers:

| Area | Methods |
|---|---|
| Lifecycle | `vault_dir`, `reopen`, `transaction`, `close` |
| Memories | `insert`, `get`, `by_hash`, `find`, `set_status`, `delete_by_keys`, `delete_by_canonical`, `count`, `count_by_status` |
| Keyword search | `fts_search` |
| Vector search | `knn`, `vec_status`, `rebuild_vec` |
| Documents | `doc_threshold`, `read_content`, `sweep_orphan_docs`, `doc_stats` |

Scans take a `MemoryQuery` (exact-match filters, a key to exclude, and
updated/archived/expiry bounds), never SQL. Rows are read-only mappings
carrying `MEMORY_FIELDS`. `row["content"]` is `None` when the text is stored
as a document; `read_content(row)` always returns the full text and raises
`ContentIntegrityError` if it is missing or altered.

Sync packs (`cairn export` / `import`, `cairn serve` push/pull) are built from
`find(..., with_embedding=True)` and `insert`, so they work on any backend
that passes the contract.

## Adding a backend

1. Subclass `StorageBackend` and override every member.
2. Add an opener to `BACKENDS` in `src/cairn/storage.py`. It is called as
   `opener(vault_dir, embed_name, dims, create=..., doc_threshold=...)` and
   must refuse a vault built with another embedder (`SpaceMismatchError`).
3. Add the name to `CONTRACT_BACKENDS` in `tests/test_storage_contract.py`,
   with a skip mark when its server is not configured, and make the whole
   contract pass unchanged. CI fails if a registered backend is not under
   contract.

Planned backends: Postgres with pgvector (#25), as part of bring-your-own
server (#26).
