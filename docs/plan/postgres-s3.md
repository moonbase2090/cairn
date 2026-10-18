# Postgres and S3 work under issue #26

Issue #26 groups remote hosting, storage backends, and backups. This document
tracks the storage and backup work in issues #23, #24, and #25. All three are
already implemented in Cairn 0.11.0 on `main`. The PR breakdown records the
scope, files, test plan, and proof for each shipped change.

## Status on `main`

| Issue | Work | Status at Cairn 0.11.0 |
| --- | --- | --- |
| [#24](https://github.com/moonbase2090/cairn/issues/24) | Storage backend interface and configuration | Complete in [PR #28](https://github.com/moonbase2090/cairn/pull/28) and [PR #29](https://github.com/moonbase2090/cairn/pull/29). |
| [#25](https://github.com/moonbase2090/cairn/issues/25) | PostgreSQL and pgvector backend | Complete in [PR #30](https://github.com/moonbase2090/cairn/pull/30). |
| [#23](https://github.com/moonbase2090/cairn/issues/23) | S3-compatible SQLite backups and point-in-time restore | Complete in [PR #41](https://github.com/moonbase2090/cairn/pull/41). |
| [#22](https://github.com/moonbase2090/cairn/issues/22) | Remote Cairn server | Still open. It is outside this storage and backup plan. |

PR [#53](https://github.com/moonbase2090/cairn/pull/53) released these
features on `main` as Cairn 0.11.0. The current
[`docs/STORAGE.md`](../STORAGE.md), [`docs/BACKUPS.md`](../BACKUPS.md), and
[`docs/HOSTING.md`](../HOSTING.md) describe the released behavior.

PR [#60](https://github.com/moonbase2090/cairn/pull/60) adds a separate,
opt-in AWS live storage backend using DynamoDB and S3. It merged to `develop`
after the 0.11.0 release. It is not present at this document's `main` base,
`de4d0c4`. The AWS backend stores live data. The backup feature in #23 uses
Litestream to back up SQLite. These are separate paths.

## Shipped PR breakdown

### PR #28: Add the storage interface and SQLite adapter

**Scope.** Define `StorageBackend` and `MemoryQuery`, move `Vault` behind the
interface, and route shared callers through it. Keep SQLite as the only
registered backend in this change.

**Files.** `src/cairn/storage.py`, `src/cairn/store.py`,
`src/cairn/client.py`, `src/cairn/cli.py`, `src/cairn/galaxy.py`,
`src/cairn/mcp_server.py`, `src/cairn/serve.py`,
`tests/test_storage_contract.py`, `pyproject.toml`, `CHANGELOG.md`, and
`tests/test_embedd.py`.

**Test plan.** Run the existing suite unchanged. Run the shared storage
contract against SQLite. Check that every registered backend overrides the
contract and appears in the contract suite.

**Proof.** [PR #28](https://github.com/moonbase2090/cairn/pull/28) reports
127 tests passed and 2 skipped, with `ruff check .` and Scorecard passing.
Its `build`, `check`, `scorecard`, and `sc` checks passed.

### PR #29: Select the storage backend from configuration

**Scope.** Read `[storage] backend` from vault or user configuration. Keep
SQLite as the default. Reject unknown or malformed storage settings and report
the active backend from `cairn doctor`. Document the interface and backend
configuration.

**Files.** `src/cairn/cli.py`, `src/cairn/mcp_server.py`,
`tests/test_storage_config.py`, `docs/STORAGE.md`, `README.md`,
`pyproject.toml`, and `CHANGELOG.md`.

**Test plan.** Run `tests/test_storage_config.py` and the full test suite. Check
vault-level and user-level configuration, the SQLite default, malformed
settings, unknown backend names, and `cairn doctor` output.

**Proof.** [PR #29](https://github.com/moonbase2090/cairn/pull/29) merged with
its `build`, `check`, `scorecard`, and `sc` checks passing. The current
[`docs/STORAGE.md`](../STORAGE.md) documents the lookup order and errors.

### PR #30: Add PostgreSQL and pgvector

**Scope.** Add the PostgreSQL backend, schema migrations, PostgreSQL
full-text search, and pgvector cosine search. Run the shared backend contract
against SQLite and PostgreSQL. Verify that separate Cairn servers share one
PostgreSQL vault.

**Files.** `src/cairn/postgres.py`, `src/cairn/storage.py`,
`src/cairn/cli.py`, `src/cairn/mcp_server.py`,
`tests/test_postgres_backend.py`, `tests/test_storage_contract.py`,
`tests/test_storage_config.py`, `.github/workflows/ci.yml`,
`.github/workflows/scorecard.yml`, `pyproject.toml`, `uv.lock`, `README.md`,
`docs/STORAGE.md`, and `CHANGELOG.md`.

**Test plan.** Set `CAIRN_TEST_POSTGRES_URL` to a pgvector-enabled database.
Run the shared storage contract, PostgreSQL integration tests, and the full
suite. Cover concurrent initialization, two `cairn serve` instances sharing
data, pack transfer, and CLI and MCP configuration.

**Proof.** [PR #30](https://github.com/moonbase2090/cairn/pull/30) records the
PostgreSQL test command, `uv lock --check`, and `git diff --check`. Its
`build`, `check`, `scorecard`, and `sc` checks passed. The current
[`docs/STORAGE.md`](../STORAGE.md) records the exact-search behavior and the
absence of an HNSW or IVFFlat index.

### PR #41: Back up SQLite to S3-compatible storage

**Scope.** Add local-folder and S3-compatible SQLite backups with WAL
replication, periodic snapshots, `cairn backup status`, and point-in-time
restore. Back up large memory documents with the database. Keep this workflow
SQLite-only.

**Files.** `src/cairn/backup.py`, `src/cairn/cli.py`,
`tests/test_backup.py`, `tests/test_backup_minio.py`, `deploy/cairn-backup.service`,
`.github/workflows/ci.yml`, `.github/workflows/scorecard.yml`, `pyproject.toml`,
`uv.lock`, `docs/BACKUPS.md`, `docs/HOSTING.md`, `docs/STORAGE.md`, `README.md`,
and `CHANGELOG.md`.

**Test plan.** Run `tests/test_backup.py` for configuration, restore safety,
and local-target behavior. Run `tests/test_backup_minio.py` with MinIO and
Litestream. Delete the test vault, restore a chosen time, then compare memory
keys, search results, and large document content with the saved state.

**Proof.** [PR #41](https://github.com/moonbase2090/cairn/pull/41) reports
191 passed and 51 skipped with a local MinIO endpoint and Litestream 0.5.16.
The point-in-time test restored both inline memories and large documents. Its
`build`, `check`, and `sc` checks passed. The current
[`docs/BACKUPS.md`](../BACKUPS.md) states that PostgreSQL vaults do not use
this backup workflow.

## Test resources and credentials

The current CI workflow already provides the required test services. It starts
`pgvector/pgvector:pg16`, creates the `vector` extension, and sets
`CAIRN_TEST_POSTGRES_URL`. It also sets `CAIRN_TEST_MINIO_ENDPOINT` to a local
MinIO service, provides disposable MinIO test credentials, and installs
Litestream 0.5.16. No production database or AWS credentials are needed for
these tests.

For an optional check against a real S3-compatible provider, MB2090 must
provide a disposable bucket, endpoint, and short-lived credentials through the
local environment or a protected CI secret. Do not commit provider credentials
to `config.toml` or test fixtures.

## Next step

Issues #23, #24, and #25 are complete on `main`. Keep issue #22 separate from
this work. Do not start further implementation under #26 until MB2090 approves
the remaining scope.
