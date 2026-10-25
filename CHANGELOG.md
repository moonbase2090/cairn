# Changelog

Notable changes to cairn. Versions follow `pyproject.toml`; see BRANCHING.md.

## 0.15.7 - 2026-10-19

### Changed

- Pin Ruff and check staged Python files before commits.
- Simplify sync-pack validation, sync-event application, and MCP serve actions.

### Fixed

- Release packages find versioned Python executables.

## 0.15.5 - 2026-10-17

### Fixed

- Release builds publish again. The release text check now reads compressed
  files that are not tar archives and skips files bundled from the Python
  runtime and third-party packages, while still scanning Cairn's own files,
  archive metadata, and the release text.
- Release archives no longer include the build directory path in Cairn's
  package metadata or bytecode.

## 0.15.4 - 2026-10-17

### Added

- Add the Cairn Agent Skill and a gated `cairn skills install` command.
  Unix and Windows archive installers install it for detected agents while
  preserving different existing copies.
- Sync row state with revisioned event packs, resumable peer cursors, and
  retained tombstones. Add per-agent curator tokens and competing correction
  detection and resolution across CLI, HTTP sync, and MCP.
- Give each vault a stable logical ID and display name. Identity appears in
  `whoami`, memory results, and sync handshakes, and sync rejects a pack from
  a different vault before applying any change.
- `cairn bootstrap --name` adds or updates only its named MCP entry and
  refuses a name that already points at a different vault.
- Opt-in AWS storage backend (`cairn[aws]`) with a per-vault CDK stack.
  SQLite stays the default, and the HTTP sync endpoint is off by default.
- A project-local verification skill for offline CLI workflows.

### Changed

- Cairn is now licensed under the MIT license instead of MPL-2.0.
- The shell installer can add the optional FastEmbed and AWS SDK packages,
  both off by default. Runtime errors now give correct install advice.
- Releases check public release text and packaged artifacts before
  publishing.

### Fixed

- Reject credential-like content, such as private keys and access tokens,
  before it is embedded or stored. A read-only preflight reports category
  counts only.
- AWS cleanup removes exact object versions, reports individual delete
  failures, and storage falls back when the runtime cannot load SQLite
  extensions.
- Validate imported document hashes and paths, clean up files when an import
  rolls back, require an access token for Galaxy data routes, refuse sync
  redirects, skip file symlinks during ingest, mark portable imports as
  external content, and validate bootstrap IDs.

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
