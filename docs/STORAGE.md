# Storage backends

A vault has two parts:

- **The vault directory** (`.cairn/` or `$CAIRN_DIR`) on local disk. It holds
  `project.json` (project, team, agent identity), the `embedder` hint,
  `audit.jsonl`, and an optional `config.toml`.
- **The live store**: memories, keyword search, vector search, and large
  memory documents. It sits behind `cairn.storage.StorageBackend`.

`sqlite` is the default. It keeps the live store in the vault directory:
`vault.db` (tables, FTS5 index, sqlite-vec index) and `docs/` for memories
larger than the document threshold. `postgres` stores memories, documents,
full-text search data, and vectors in a PostgreSQL database with pgvector.

SQLite vaults can also replicate WAL changes to local folders or S3-compatible
storage. See [Back up a SQLite vault](BACKUPS.md) for destination setup and
point-in-time restore.

The AWS backend is opt-in. SQLite does not install or import AWS SDK packages.
Install or upgrade Cairn with `CAIRN_INSTALL_AWS=1` to add boto3 and botocore to
the Cairn tool environment. AWS setup also needs the AWS CLI, Node.js, and npm;
Cairn installs its pinned CDK dependencies under `aws/infra` when it first
synthesizes or deploys. `cairn_aws_storage` with `action: "check"` reports the
system tool versions without making AWS calls. Planning checks credentials and
synthesizes locally; applying a reviewed plan deploys the stack.

## Choosing a backend

Set `[storage] backend` in a `config.toml`:

```toml
[storage]
backend = "sqlite"
```

For PostgreSQL, install the optional dependencies and configure a connection
URL. Use a dedicated database for each vault. Every `cairn serve` instance
using the same URL shares that vault, including large memory documents.

```sh
pip install 'cairn[postgres]'
```

```toml
[storage]
backend = "postgres"
url = "postgresql://cairn:password@db.example/cairn"
```

Enable pgvector on the PostgreSQL server before you initialize the vault.
Cairn creates its tables in the connection's active schema. The role needs
`CREATE` permission there and `USAGE` permission on the schema that contains
pgvector. Cairn adds the pgvector schema to each connection's search path.
Treat the connection URL as a secret.

Cairn reads storage settings in this order:

1. `<vault dir>/config.toml`, for this vault only
2. `~/.cairn/config.toml`, for every vault on the machine
3. the default, `sqlite`

`cairn init`, every CLI command, and `cairn-mcp` use the same lookup. An
unknown name fails before anything is created or opened:

```
error: unknown storage backend 'custom'; available: postgres, sqlite
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
| Server credentials | `create_server_token`, `get_server_token`, `list_server_tokens`, `delete_server_token`, `get_agent_ids` |
| Sync | `sync_origin_id`, `export_sync_events`, `apply_sync_event`, `has_sync_event`, `has_sync_tombstone`, cursor and conflict methods |

Scans take a `MemoryQuery` (exact-match filters, a key to exclude, and
updated/archived/expiry bounds), never SQL. Rows are read-only mappings
carrying `MEMORY_FIELDS`. `row["content"]` is `None` when the text is stored
as a document; `read_content(row)` always returns the full text and raises
`ContentIntegrityError` if it is missing or altered.

`cairn export` and `cairn import` keep portable v1 snapshots. Server push and
pull use v2 events, state revisions, and tombstones. The event log, tombstone
fence, per-peer cursors, and conflict records share the backend database with
memories, so they are included in vault backups. The shared contract covers
event persistence and cursor operations for SQLite and PostgreSQL.

PostgreSQL records schema migrations in `cairn_migrations`. It uses
PostgreSQL full-text search and pgvector cosine distance. Cairn calculates
exact distances over rows that pass the metadata and expiry filters. It does
not create an HNSW or IVFFlat index, so query time grows with the number of
matching rows. Large content lives in a shared database table. SQLite keeps
large content in content-addressed files under `docs/`.

## Adding a backend

1. Subclass `StorageBackend` and override every member.
2. Add an opener to `BACKENDS` in `src/cairn/storage.py`. It is called as
   `opener(vault_dir, embed_name, dims, config, create=..., doc_threshold=...)`
   and must refuse a vault built with another embedder (`SpaceMismatchError`).
3. Add the name to `CONTRACT_BACKENDS` in `tests/test_storage_contract.py`,
   skip its fixture when its service URL is not configured, and make the whole
   contract pass unchanged. CI fails if a registered backend is not under the
   contract.

CI runs the shared backend contract against SQLite and a pgvector-enabled
PostgreSQL service.
