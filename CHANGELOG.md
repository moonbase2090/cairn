# Changelog

Notable changes to cairn. Versions follow `pyproject.toml`; see BRANCHING.md.

## [0.8.0] — 2026-09-28

### Added

- `cairn serve` reads `CAIRN_TOKEN` when `--token` is not set, and does not print a configured token at startup.
- systemd unit and Caddy hosting guide for a shared-token sync server.

## Unreleased

### Added

- Storage backend interface (`cairn.storage.StorageBackend`, #24). Memories,
  keyword search, vector search, documents, and the data behind sync packs
  now go through one interface, opened with `cairn.storage.open_backend()`.
  SQLite (`cairn.store.Vault`) is the default backend. PostgreSQL with
  pgvector is available as an optional backend. An unknown backend name fails
  with a clear error.
- `[storage] backend` config key (#24), read from the vault's `config.toml`,
  then `~/.cairn/config.toml`, defaulting to `sqlite`. `cairn init`, the CLI,
  and `cairn-mcp` honour it; PostgreSQL also reads a connection `url`. An
  unknown or malformed setting is an error.
  `cairn doctor` reports the backend as `storage`. See docs/STORAGE.md.
- Storage contract test suite (`tests/test_storage_contract.py`) that every
  backend must pass. It runs against SQLite and PostgreSQL in CI.
- PostgreSQL storage (#25), using database migrations, PostgreSQL full-text
  search, and pgvector cosine distance. Multiple `cairn serve` instances can
  share the same configured vault.

### Changed

- `CairnClient`, `cairn serve`, the galaxy view, the CLI, and the MCP server
  use structured `MemoryQuery` scans instead of SQL fragments, and no longer
  touch the SQLite connection directly.
