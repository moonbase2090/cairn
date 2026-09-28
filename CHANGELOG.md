# Changelog

Notable changes to cairn. Versions follow `pyproject.toml`; see BRANCHING.md.

## 0.7.0 - 2026-09-28

### Added

- Choose a vault's storage backend with `[storage] backend` and its connection
  `url` in `config.toml`. SQLite remains the default. PostgreSQL with pgvector
  supports full-text and vector search, and can share a vault across multiple
  `cairn serve` instances. `cairn doctor` reports the active backend.
- Require TLS 1.2 or later and a bearer token when `cairn serve` listens beyond
  localhost.
- Include a `SHA256SUMS` file with release downloads so you can verify the
  archives. macOS release archives are signed and notarized. Windows archives
  can be Authenticode-signed when signing is configured.

### Changed

- The CLI, `cairn-mcp`, `cairn serve`, and the galaxy view use the shared
  storage layer, so they work with SQLite or PostgreSQL.
- Prevent socket path errors when `cairn-embedd` starts on macOS.
