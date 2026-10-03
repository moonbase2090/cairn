# Changelog

Notable changes to cairn. Versions follow `pyproject.toml`; see BRANCHING.md.

## 0.12.1 - Unreleased

### Added

- Add the Cairn Agent Skill and a gated `cairn skills install` command.
  Unix and Windows archive installers install it for detected agents while
  preserving different existing copies.
- Sync row state with revisioned event packs, resumable peer cursors, and
  retained tombstones. Add per-agent curator tokens and competing correction
  detection and resolution across CLI, HTTP sync, and MCP.

## 0.11.0 - 2026-09-30

### Added

- Per-agent bearer tokens for remote vault servers, with commands to create,
  list, and revoke tokens. Each token limits writes to its assigned agent.

### Changed

- Release checks use Scorecard v0.1.5 and Node 24-compatible GitHub Actions.

## 0.10.0 - 2026-09-29

### Added

- Continuous SQLite backups to local folders and S3-compatible storage, with
  periodic snapshots, external memory documents, backup status, and
  point-in-time restore.

## 0.9.0 - 2026-09-29

### Added

- `CAIRN_URL` supplies the default server URL for `cairn push` and `cairn pull`.
- `cairn serve` reads its bearer token from `CAIRN_TOKEN` when `--token` is not
  set, so the same variable configures both the server and client sync.
- systemd unit and Caddy hosting guide for a shared-token sync server.

### Changed

- Startup output no longer prints a configured bearer token.

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
