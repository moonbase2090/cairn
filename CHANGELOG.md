# Changelog

Notable changes to cairn. Versions follow `pyproject.toml`; see BRANCHING.md.

## Unreleased

### Added

- Storage backend interface (`cairn.storage.StorageBackend`, #24). Memories,
  keyword search, vector search, documents, and the data behind sync packs
  now go through one interface, opened with `cairn.storage.open_backend()`.
  SQLite (`cairn.store.Vault`) is the default and only backend, with no
  change in behaviour. An unknown backend name fails with a clear error.
- Storage contract test suite (`tests/test_storage_contract.py`) that every
  backend must pass. It runs against SQLite in CI.

### Changed

- `CairnClient`, `cairn serve`, the galaxy view, the CLI, and the MCP server
  use structured `MemoryQuery` scans instead of SQL fragments, and no longer
  touch the SQLite connection directly.
