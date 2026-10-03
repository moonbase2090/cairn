# Sync updates

Status: proposal. This document describes a change to pack and server behavior. It does not implement that change.

## The current import loses corrections and archives

`tests/test_sync_updates.py::test_sync_propagates_existing_row_status_changes` syncs two SQLite vaults through `export()` and `import_pack()`. It covers both a correction and an archive. The focused run, `uv run pytest tests/test_sync_updates.py -q`, failed in both cases: the source changed the old row's status, but the peer kept it active.

The cause is in `CairnClient.import_pack()`. When a key already exists, import increments `skipped` and leaves the row untouched. `export()` sends snapshots, and `push` sends a full export every time. The peer never sees a state transition for an existing key.

## Keep content version separate from sync revision

Cairn's existing `version` identifies a semantic content version. A correction creates a new key and marks the prior key `superseded`. Keep that field for its current purpose. Add a separate `state_revision` for changes to a row's status or mutable metadata.

Every mutation writes a complete, immutable sync event. An event carries:

- `event_id`, derived from the writer's stable `origin_id` and monotonically increasing `origin_seq`.
- `change_set_id`, shared by all row events from one Cairn operation, plus an event count so a page cannot split the operation.
- `origin_id` and `origin_seq`, which let a receiver deduplicate and resume a feed. Generate `origin_id` for one writable vault instance. Regenerate it when a vault copy becomes an independent writer.
- `key`, `state_revision`, `updated_at`, and the writer identity.
- A `snapshot` event with the complete memory row, content, and embedding when a row is created.
- A `state` event with the row's new status and mutable metadata when a row changes state.
- A `delete` event with a tombstone when a row is physically removed.

A writer increments `state_revision` from the highest revision it has observed for that key. Receivers compare `(state_revision, updated_at, origin_id, event_id)` lexicographically and keep the greatest value. The tuple gives peers one deterministic winner when they make concurrent changes from the same revision. `updated_at` remains an epoch timestamp for compatibility. The writer identity and event ID settle ties when clocks have the same second.

Treat a memory key's content as immutable. Cairn keys include a digest and a semantic version. A correction therefore creates a new key and a `supersedes` link. If an incoming event uses an existing key but has a different `content_hash`, reject it as an integrity conflict. Do not overwrite content under that key.

A later restore is a new state event with a greater revision. It can reactivate an archived or superseded row. Sync does not impose a permanent status order; it applies the same revision rule to archive, supersede, and restore.

## Commit row changes with their events

Write each row mutation and its event in one backend transaction. Centralize event creation in the memory lifecycle methods so `store_memory`, `archive_memory`, `restore_memory`, and `gc` cannot update a row without recording the change.

A receiver applies a change set in one backend transaction. It applies each event only when that row's comparison tuple wins, records every event ID, and advances the cursor together. Duplicate events are no-ops. Keep all events for a change set in the same page. If a set exceeds a provider's transaction limit, split the source operation into smaller change sets before commit. A row state event for an unknown key is an error that asks the sender to provide a baseline snapshot; it must not create a partial row.

## Send deltas with resumable cursors

Use a version 2 pack with events and an opaque cursor. A cursor records the highest contiguous sequence received for each origin. Keep each origin's feed in sequence order. If a receiver finds a gap, it asks for the missing events and does not advance that origin's cursor. Store the cursor only after the receiver commits every complete change set in the page. It can then safely retry the page after a crash.

On the first push or pull, exchange a full snapshot and establish cursors. Include retained tombstones in a version 2 bootstrap. Later `push` calls send only local events the peer has not acknowledged. Later `pull` calls request events after the saved cursor. Keep per-peer cursors in the local vault configuration or backend. When a cursor predates retained history, require a fresh snapshot instead of returning an incomplete delta. A normal snapshot merge stays additive. If a peer has lost its cursor and needs old deletions reconciled, rebuild into a fresh vault or request an explicit, confirmed replacement of that sync scope.

Keep `cairn export` as a portable full snapshot. Add cursor-based export for sync. Keep `--since EPOCH` as a compatibility path for version 1 packs and inclusive timestamp queries. Because existing `updated_at` values have second precision, that path can repeat rows with the same timestamp. Event IDs make those repeats safe to deduplicate. New clients should use opaque cursors.

The event log must retain a change until registered peers have acknowledged it. Operators can remove an abandoned peer, which also removes its acknowledgement state. Unregistered file copies receive a full snapshot and cannot claim that they received a deletion they never saw.

## Make hard deletion visible

A row removed by `gc`, expiration cleanup, or `purge_memory` needs a tombstone event. A tombstone records the key, canonical ID, final revision, deletion time, and reason. It is not a searchable memory row.

Do not remove a tombstone while a registered peer may still need it. Remove it only after every registered peer has passed its sequence, or after an operator removes a peer and requires it to bootstrap again. Then remove any unreferenced content document. This avoids restoring a deleted row from an old snapshot.

## Apply per-agent ownership to updates

A per-agent token may create rows for its assigned agent and update rows owned by that same agent. It may not archive, restore, supersede, or delete a row owned by another agent. A correction that links to another agent's row requires a separate curator permission. A shared token keeps its existing shared-vault permissions.

Validate every event's claimed owner against the token and the stored owner of an existing key. Allow an unchanged foreign row to round-trip after a pull. Reject a foreign state change even when its pack also contains the row's full snapshot. This preserves the current server rule that a client cannot create another agent's identity.

## Read old packs without inventing updates

Version 2 readers continue to accept `cairn-export-1`. Treat a version 1 row as a baseline with `state_revision = 0` only when the key is absent. Preserve its status and content on that initial insert. Keep the existing insert-only rule when the key already exists because version 1 has no reliable update revision or tombstone.

Version 1 readers reject version 2 packs with a clear unsupported-format error. A schema migration seeds the event log from existing rows before enabling delta sync. It must not infer that an absent row was deleted. Cairn has no tombstones for rows physically deleted before this migration, so those past deletions cannot be reconstructed from an old vault. New hard deletes must emit tombstones.

## Expose sync operations to agents

The MCP server and the installed Cairn skill must cover the same sync and server operations. Keep the current CLI for scripts and manual use. Provider-tool installation and cloud setup are specified in [cloud-backends.md](cloud-backends.md).

Add MCP tools for `cairn_sync` with `status`, `push`, and `pull` actions; `cairn_serve` with `start`, `status`, `health`, and `stop` actions; and `cairn_token` with `create`, `list`, and `revoke` actions. The server start action manages a child process and returns its address and process state instead of blocking the MCP request.

Add skill sections for first sync, delta cursors, server lifecycle, token ownership, and recovery from a stale cursor. Keep export and import available for portable file packs. The skill directs the agent to the MCP tools for normal operations.

## Decisions still needed before implementation

Choose event-log retention storage and peer removal UX for SQLite and PostgreSQL. Define how a user approves curator permission. Decide whether cursor state belongs in per-vault config or a dedicated sync-state table. Keep these choices inside the storage and sync contract review; do not weaken the event ordering or ownership rules above.
