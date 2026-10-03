# Sync updates

Status: implemented. This document records the SQLite and PostgreSQL sync
contract.

## The original bug

`CairnClient.import_pack()` used to skip every key already in the destination.
That preserved the original content but also discarded status changes. A peer
could keep surfacing a memory after its owner corrected or archived it.
`tests/test_sync_updates.py` now covers the state transition and the v1
compatibility rule.

## State revisions and events

The semantic `version` still identifies a content correction. `state_revision`
tracks later changes to that row. SQLite and PostgreSQL keep the event log,
tombstones, peer cursors, and conflict records in the same database as memories.
Vault migrations create a stable `origin_id` and seed a snapshot event for each
row that existed before event sync.

Each event has a stable `event_id` derived from the writer origin and sequence.
Snapshot events carry the memory, full content, and embedding. State events
carry status and state metadata. Tombstone events record a hard deletion. An
imported event keeps its original ID and is added to the destination's local
feed, so another peer can receive it without generating a duplicate mutation.

Receivers compare `(state_revision, updated_at, state_origin, state_event_id)`
and apply the greatest tuple. Content under a key is immutable; a different
`content_hash` is rejected. A state event for a missing key is rejected until a
snapshot arrives. A later restore is another state event and can reactivate an
archived or superseded row.

## Delta packs and cursors

`cairn export` still writes a portable `cairn-export-1` snapshot. Importing v1
adds only keys that are missing; it never updates an existing row. A remembered
tombstone also blocks an old v1 snapshot from recreating a deleted key.
If a user stores the same content again after purge, Cairn allocates the next
unused versioned key so the new memory is not blocked by the old tombstone.

Server push and pull use `cairn-sync-2` event pages. Each page has an `after`
cursor and a cursor at its final event. Pages contain at most 100,000 events;
the next call resumes from the returned cursor. The receiver applies the page
and advances its per-peer, per-direction cursor in one transaction. Replaying a
page is safe because event IDs are unique. Existing `pull --since EPOCH` stays
as a v1 compatibility path.

The event log and tombstones are retained. This keeps hard deletes available to
peers that have not synced yet and lets cursors travel with vault backups.
Events are not currently compacted and there is no peer removal command. A
legacy vault cannot recreate tombstones for rows physically deleted before the
migration.

## Ownership and curator tokens

A per-agent token can create rows for its assigned agent and change that
agent's rows. It cannot change another agent's state, create rows under another
identity, or supersede another agent's row. Create a curator token with
`cairn token create --agent NAME --curator` to allow cross-agent state changes
and correction conflict resolution. Token listings expose the curator role;
token secrets are still shown only once.

An unchanged foreign row can round-trip after a pull. A new foreign state event
is checked against the token role and the stored row owner. Shared-token mode
keeps its existing shared-vault behavior.

## Competing corrections

If more than one active row supersedes the same key, Cairn records and reports
a competing correction. Import leaves every competitor active. Check them with
`cairn conflicts list`, `cairn sync status`, `cairn doctor`, or the
`cairn_conflicts` MCP tool. Choose a winner with `cairn conflicts resolve` or
the MCP tool. The winner remains active; Cairn marks the other rows
`superseded` in ordinary state events that sync to peers. The owner of the
winner can resolve a same-agent conflict. Cross-agent resolution requires a
curator token.

## Agent operations

The CLI, MCP server, and installed skill expose push, pull, token create/list/
revoke, sync status, and conflict list/resolve. MCP tools are `cairn_sync`,
`cairn_serve` (start/status/health/stop), `cairn_token`, and `cairn_conflicts`.
The skill directs agents to these tools for normal sync operations and keeps
file export/import available for portable snapshots.
