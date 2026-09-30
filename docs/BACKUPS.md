# Back up a SQLite vault

Cairn can continuously back up a SQLite vault to one or more local folders or
S3-compatible buckets. The live `vault.db` stays on local disk. Backups contain
SQLite WAL changes and periodic snapshots, so Cairn does not upload the whole
database after every write.

Install [Litestream 0.5.16 or later](https://litestream.io/install/) and the
S3 backup dependency on the machine that can read the vault:

```sh
pip install 'cairn[backup]'
```

Cairn uses Litestream to replicate SQLite changes and restore a vault. Memories
larger than the vault's document threshold are stored in separate content files;
Cairn backs up the files referenced by the database alongside each destination.
Backups are available for SQLite vaults; PostgreSQL vaults do not use this
workflow.

## Configure destinations

Add one `[[backup]]` table for each destination in `config.toml`:

```toml
[[backup]]
name = "primary"
kind = "s3"
bucket = "my-cairn-backups"
path = "vaults/project-a"
region = "us-east-1"
endpoint = "https://s3.example.com" # omit for AWS S3
sync_interval = 1.0
snapshot_interval = "24h"
retention = "168h"
l0_retention = "24h"

[[backup]]
name = "local-copy"
kind = "file"
path = "../cairn-backups/project-a"
sync_interval = 1.0
snapshot_interval = "24h"
retention = "168h"
l0_retention = "24h"
```

For S3-compatible storage, `endpoint` is the service's HTTP or HTTPS URL.
`region` defaults to `us-east-1`; set the region required by your provider.
Create the bucket before starting backup replication. Use HTTPS for remote
services. A local MinIO endpoint can use HTTP for development and testing.

For a `file` destination, `path` names a folder outside the live vault. Relative
paths are resolved from the directory containing `config.toml`. If an S3
destination omits `path`, Cairn creates a stable prefix from the vault's
location. Give each destination a unique `name` to select it during restore.

Cairn checks for backup settings in the vault's `config.toml`, then
`~/.cairn/config.toml`, then `$XDG_CONFIG_HOME/cairn/config.toml` (by default,
`~/.config/cairn/config.toml`). The first file with `[[backup]]` entries is
used. For recovery after the default `~/.cairn` vault directory is removed,
keep a copy of these settings under `~/.config/cairn/config.toml`.

Keep S3 credentials outside `config.toml`. Litestream and Cairn use the
standard AWS credential chain, including AWS environment variables, shared
profiles, and workload roles. Litestream also accepts
`LITESTREAM_ACCESS_KEY_ID` and `LITESTREAM_SECRET_ACCESS_KEY`. Cairn does not
print credentials.

`sync_interval` is the number of seconds between replication attempts.
`snapshot_interval` sets how often Cairn requests a full snapshot;
`retention` controls how long snapshots are kept; `l0_retention` keeps recent
transaction files for finer restore points. Durations use seconds, minutes, or
hours, such as `30s`, `5m`, or `24h`.

## Run replication

Keep this command running while Cairn writes to the vault:

```sh
cairn backup replicate
```

It checks each configured destination in turn and retries on the next cycle
after a failed upload. Run it as a service alongside `cairn serve` for a shared
server vault. The included systemd unit is
[`deploy/cairn-backup.service`](../deploy/cairn-backup.service).

View each destination's last successful backup with:

```sh
cairn backup status
```

The status command works while the vault directory is missing. If the vault
directory may be removed during recovery, keep the backup settings outside it.

## Restore

Restore the latest backup to the configured vault location:

```sh
cairn restore --from primary
```

Restore to a chosen time using an ISO 8601 timestamp with a timezone:

```sh
cairn restore --from primary --at 2026-09-29T18:00:00Z
```

Restore writes only when `vault.db` and its SQLite sidecar files are absent.
Move an existing database aside before restoring so Cairn cannot replace live
data by accident. Litestream restores the latest retained committed update at
or before the requested time. Restore points follow SQLite transaction and
retention boundaries, so they may be earlier than the requested timestamp.
Each restore runs a full SQLite integrity check.

The SQLite database follows the selected point in time. External documents are
content-addressed and retained at the destination when no longer referenced, so
the restored database can use the matching files. If the whole vault directory
was removed, Cairn recreates its embedder hint and project identity from the
restored database. The audit log is stored separately and is not part of this
backup.

For recovery guidance and service setup, see
[Run a Cairn sync server](HOSTING.md).
