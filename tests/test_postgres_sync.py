"""Unit tests for PostgreSQL sync-event validation without a live database."""

from __future__ import annotations

import base64
from contextlib import nullcontext

import numpy as np
import pytest

from cairn.postgres import PostgresVault
from cairn.storage import MEMORY_FIELDS

BASE_KEY = "base-memory"
COMPETITORS = ["correction-a", "correction-b"]
WINNER_KEY = "correction-b"


def make_backend(monkeypatch, *, stored=(), dynamic=(), rows=None):
    backend = object.__new__(PostgresVault)
    monkeypatch.setattr(
        backend,
        "list_sync_conflicts",
        lambda include_resolved=False: list(stored),
    )
    monkeypatch.setattr(
        backend,
        "list_competing_corrections",
        lambda: list(dynamic),
    )
    monkeypatch.setattr(backend, "get", lambda key: (rows or {}).get(key))
    return backend


def valid_event():
    return {
        "kind": "state",
        "key": "correction-a",
        "status": "superseded",
    }


def valid_resolution():
    return {
        "base_key": BASE_KEY,
        "winner_key": WINNER_KEY,
        "competitor_keys": COMPETITORS.copy(),
        "resolved_by": "agent-a",
    }


def matching_conflict():
    return {
        "base_key": BASE_KEY,
        "competitor_keys": COMPETITORS.copy(),
    }


def matching_rows():
    return {
        "correction-a": {"supersedes": BASE_KEY},
        WINNER_KEY: {"supersedes": BASE_KEY, "status": "active"},
    }


@pytest.mark.parametrize(
    ("event", "resolution", "message"),
    [
        ({"kind": "snapshot", "status": "superseded"}, {}, "supersede state event"),
        ({"kind": "state", "status": "archived"}, {}, "supersede state event"),
        (valid_event(), None, "supersede state event"),
    ],
)
def test_rejects_non_supersede_resolution_events(
    monkeypatch, event, resolution, message
):
    backend = make_backend(monkeypatch)

    with pytest.raises(ValueError, match=message):
        backend._validate_resolution_event(event, resolution)


@pytest.mark.parametrize(
    ("event", "resolution"),
    [
        (valid_event(), {**valid_resolution(), "base_key": None}),
        (valid_event(), {**valid_resolution(), "winner_key": None}),
        (valid_event(), {**valid_resolution(), "competitor_keys": None}),
        (valid_event(), {**valid_resolution(), "competitor_keys": ["correction-a"]}),
        (
            valid_event(),
            {**valid_resolution(), "competitor_keys": ["correction-a", 4]},
        ),
        (
            valid_event(),
            {
                **valid_resolution(),
                "competitor_keys": ["correction-a", WINNER_KEY, WINNER_KEY],
            },
        ),
        (valid_event(), {**valid_resolution(), "resolved_by": None}),
        (valid_event(), {**valid_resolution(), "resolved_by": ""}),
        ({**valid_event(), "key": "outside"}, valid_resolution()),
        (
            valid_event(),
            {**valid_resolution(), "winner_key": "outside"},
        ),
        (
            {**valid_event(), "key": WINNER_KEY},
            valid_resolution(),
        ),
    ],
)
def test_rejects_malformed_resolution_members(monkeypatch, event, resolution):
    backend = make_backend(monkeypatch)

    with pytest.raises(ValueError, match="invalid competing correction resolution"):
        backend._validate_resolution_event(event, resolution)


@pytest.mark.parametrize(
    ("stored", "dynamic", "rows"),
    [
        ([], [], matching_rows()),
        (
            [{"base_key": "another-base", "competitor_keys": COMPETITORS}],
            [],
            matching_rows(),
        ),
        (
            [],
            [{"base_key": BASE_KEY, "competitor_keys": ["other-a", "other-b"]}],
            matching_rows(),
        ),
        ([matching_conflict()], [], {"correction-a": {"supersedes": BASE_KEY}}),
        (
            [matching_conflict()],
            [],
            {
                "correction-a": {"supersedes": BASE_KEY},
                WINNER_KEY: {"supersedes": BASE_KEY, "status": "superseded"},
            },
        ),
        (
            [matching_conflict()],
            [],
            {
                "correction-a": {"supersedes": BASE_KEY},
                WINNER_KEY: {"supersedes": "different-base", "status": "active"},
            },
        ),
    ],
)
def test_rejects_resolutions_without_matching_active_corrections(
    monkeypatch, stored, dynamic, rows
):
    backend = make_backend(monkeypatch, stored=stored, dynamic=dynamic, rows=rows)

    with pytest.raises(
        ValueError,
        match="conflict resolution does not match an active competing correction",
    ):
        backend._validate_resolution_event(valid_event(), valid_resolution())


@pytest.mark.parametrize("conflict_source", ["stored", "dynamic"])
def test_accepts_resolution_for_matching_active_corrections(
    monkeypatch, conflict_source
):
    stored = [matching_conflict()] if conflict_source == "stored" else []
    dynamic = [matching_conflict()] if conflict_source == "dynamic" else []
    backend = make_backend(
        monkeypatch,
        stored=stored,
        dynamic=dynamic,
        rows=matching_rows(),
    )

    assert backend._validate_resolution_event(valid_event(), valid_resolution()) is None


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


def memory_row(**overrides):
    row = {field: None for field in MEMORY_FIELDS}
    row.update(
        {
            "key": "memory-a",
            "canonical_id": "memory-a",
            "content": "stored fact",
            "content_hash": "sha256:stored",
            "status": "active",
            "updated_at": 10,
            "state_revision": 1,
            "state_origin": "origin-a",
            "state_event_id": "origin-a:1",
            "supersedes": None,
            "embedding": np.array([1.0, 0.0], dtype=np.float32),
            "rowid": 1,
            "content_ref": None,
        }
    )
    row.update(overrides)
    return row


def make_apply_backend(monkeypatch, *, memories=None, event_ids=(), tombstones=()):
    backend = object.__new__(PostgresVault)
    memories = memories if memories is not None else {}
    event_ids = set(event_ids)
    tombstones = set(tombstones)
    appended = []
    inserted = []
    deleted_orphans = []
    resolutions = []

    def execute(query, params=()):
        statement = " ".join(query.split())
        if statement.startswith("SELECT 1 FROM cairn_sync_events"):
            return Cursor({"event_id": params[0]} if params[0] in event_ids else None)
        if statement.startswith("SELECT 1 FROM cairn_sync_tombstones"):
            return Cursor({"key": params[0]} if params[0] in tombstones else None)
        if statement.startswith("SELECT rowid,") and "FOR UPDATE" in statement:
            return Cursor(memories.get(params[0]))
        if statement.startswith("UPDATE cairn_memories SET status=%s"):
            key = params[-1]
            row = memories[key]
            fields = (
                (
                    "status",
                    "updated_at",
                    "archived_at",
                    "expires_at",
                    "state_revision",
                    "state_origin",
                    "state_event_id",
                )
                if "expires_at=%s" in statement
                else (
                    "status",
                    "updated_at",
                    "archived_at",
                    "state_revision",
                    "state_origin",
                    "state_event_id",
                )
            )
            row.update(zip(fields, params[:-1]))
            return Cursor()
        if statement.startswith("DELETE FROM cairn_memories WHERE key=%s"):
            memories.pop(params[0], None)
            return Cursor()
        raise AssertionError(f"unexpected query: {statement}")

    def insert(rec, vector):
        row = memory_row(**rec)
        row["embedding"] = np.asarray(vector, dtype=np.float32)
        row["rowid"] = len(memories) + 1
        memories[rec["key"]] = row
        inserted.append((dict(rec), np.asarray(vector, dtype=np.float32)))
        return row["rowid"]

    monkeypatch.setattr(backend, "transaction", lambda: nullcontext())
    monkeypatch.setattr(backend, "_execute", execute)
    monkeypatch.setattr(backend, "insert", insert)
    monkeypatch.setattr(backend, "_append_sync_event", appended.append)
    monkeypatch.setattr(
        backend, "_remember_tombstone", lambda event: tombstones.add(event["key"])
    )
    monkeypatch.setattr(
        backend, "_delete_orphan_docs", lambda: deleted_orphans.append(True) or 0
    )
    monkeypatch.setattr(backend, "_record_resolution_event", resolutions.append)
    return backend, memories, appended, inserted, deleted_orphans, resolutions


def snapshot_event(*, key="memory-a", content_hash="sha256:stored", **overrides):
    vector = np.array([0.25, 0.75], dtype=np.float32)
    snapshot = {
        **{field: None for field in MEMORY_FIELDS},
        "key": key,
        "canonical_id": key,
        "content": "incoming fact",
        "content_hash": content_hash,
        "status": "active",
        "updated_at": 20,
        "state_revision": 2,
        "state_origin": "origin-b",
        "state_event_id": "origin-b:2",
        "embedding": base64.b64encode(vector.tobytes()).decode("ascii"),
    }
    return {
        "event_id": "origin-b:2",
        "origin_id": "origin-b",
        "kind": "snapshot",
        "key": key,
        "state_revision": 2,
        "updated_at": 20,
        "state_origin": "origin-b",
        "state_event_id": "origin-b:2",
        "snapshot": snapshot,
        **overrides,
    }


@pytest.mark.parametrize("event", [None, {}, {"event_id": 7}])
def test_apply_sync_event_rejects_missing_event_id(monkeypatch, event):
    backend, *_ = make_apply_backend(monkeypatch)

    with pytest.raises(ValueError, match="missing event_id"):
        backend.apply_sync_event(event)


def test_apply_sync_event_skips_duplicate_without_reapplying(monkeypatch):
    backend, memories, appended, inserted, *_ = make_apply_backend(
        monkeypatch, event_ids={"origin-b:2"}
    )

    assert backend.apply_sync_event(snapshot_event()) == "skipped"
    assert memories == {}
    assert appended == []
    assert inserted == []


@pytest.mark.parametrize(
    "event",
    [
        {"event_id": "origin:1", "kind": "unknown", "key": "memory-a"},
        {"event_id": "origin:1", "kind": "state", "key": 4},
    ],
)
def test_apply_sync_event_rejects_invalid_kind_or_key(monkeypatch, event):
    backend, *_ = make_apply_backend(monkeypatch)

    with pytest.raises(ValueError, match="invalid sync event kind or key"):
        backend.apply_sync_event(event)


@pytest.mark.parametrize("snapshot", [None, {"key": "another-memory"}])
def test_apply_sync_event_rejects_invalid_snapshot(monkeypatch, snapshot):
    backend, *_ = make_apply_backend(monkeypatch)
    event = snapshot_event()
    event["snapshot"] = snapshot

    with pytest.raises(ValueError, match="invalid snapshot sync event"):
        backend.apply_sync_event(event)


def test_apply_sync_event_rejects_snapshot_content_hash_change(monkeypatch):
    backend, *_ = make_apply_backend(monkeypatch, memories={"memory-a": memory_row()})

    with pytest.raises(ValueError, match="sync integrity conflict"):
        backend.apply_sync_event(snapshot_event(content_hash="sha256:changed"))


def test_apply_sync_event_records_snapshot_skipped_by_tombstone(monkeypatch):
    backend, memories, appended, inserted, *_ = make_apply_backend(
        monkeypatch, tombstones={"memory-a"}
    )
    event = snapshot_event()

    assert backend.apply_sync_event(event) == "skipped"
    assert memories == {}
    assert appended == [event]
    assert inserted == []


def test_apply_sync_event_rejects_snapshot_without_embedding(monkeypatch):
    backend, *_ = make_apply_backend(monkeypatch)
    event = snapshot_event()
    event["snapshot"]["embedding"] = None

    with pytest.raises(ValueError, match="has no embedding"):
        backend.apply_sync_event(event)


def test_apply_sync_event_inserts_new_snapshot(monkeypatch):
    backend, memories, appended, inserted, *_ = make_apply_backend(monkeypatch)
    event = snapshot_event(key="new-memory", content_hash="sha256:new")

    assert backend.apply_sync_event(event) == "added"
    assert memories["new-memory"]["content"] == "incoming fact"
    assert inserted[0][0]["key"] == "new-memory"
    assert inserted[0][0]["content"] == "incoming fact"
    assert inserted[0][1].tolist() == [0.25, 0.75]
    assert appended == [event]


def test_apply_sync_event_updates_newer_snapshot_state(monkeypatch):
    backend, memories, appended, *_ = make_apply_backend(
        monkeypatch, memories={"memory-a": memory_row()}
    )
    event = snapshot_event()
    event["snapshot"].update(
        {"status": "archived", "archived_at": 20, "expires_at": 30}
    )

    assert backend.apply_sync_event(event) == "updated"
    assert memories["memory-a"]["status"] == "archived"
    assert memories["memory-a"]["updated_at"] == 20
    assert memories["memory-a"]["state_event_id"] == "origin-b:2"
    assert appended == [event]


def test_apply_sync_event_skips_older_snapshot_state(monkeypatch):
    backend, memories, appended, *_ = make_apply_backend(
        monkeypatch, memories={"memory-a": memory_row(state_revision=5)}
    )
    event = snapshot_event()

    assert backend.apply_sync_event(event) == "skipped"
    assert memories["memory-a"]["status"] == "active"
    assert memories["memory-a"]["state_revision"] == 5
    assert appended == [event]


def state_event(**overrides):
    return {
        "event_id": "origin-b:2",
        "origin_id": "origin-b",
        "kind": "state",
        "key": "memory-a",
        "state_revision": 2,
        "updated_at": 20,
        "state_origin": "origin-b",
        "state_event_id": "origin-b:2",
        "status": "archived",
        "archived_at": 20,
        **overrides,
    }


def test_apply_sync_event_skips_state_for_tombstoned_memory(monkeypatch):
    backend, memories, appended, *_ = make_apply_backend(
        monkeypatch, tombstones={"memory-a"}
    )
    event = state_event()

    assert backend.apply_sync_event(event) == "skipped"
    assert memories == {}
    assert appended == [event]


def test_apply_sync_event_rejects_state_for_missing_memory(monkeypatch):
    backend, *_ = make_apply_backend(monkeypatch)

    with pytest.raises(ValueError, match="send a snapshot first"):
        backend.apply_sync_event(state_event())


def test_apply_sync_event_updates_newer_state_and_records_resolution(monkeypatch):
    backend, memories, appended, _, _, resolutions = make_apply_backend(
        monkeypatch, memories={"memory-a": memory_row()}
    )
    validated = []
    event = state_event(conflict_resolution={"base_key": "base-memory"})
    monkeypatch.setattr(
        backend,
        "_validate_resolution_event",
        lambda current, resolution: validated.append((current, resolution)),
    )

    assert backend.apply_sync_event(event) == "updated"
    assert memories["memory-a"]["status"] == "archived"
    assert memories["memory-a"]["archived_at"] == 20
    assert appended == [event]
    assert validated == [(event, event["conflict_resolution"])]
    assert resolutions == [event]


def test_apply_sync_event_skips_validation_for_tombstoned_resolution(monkeypatch):
    backend, _, appended, *_ = make_apply_backend(monkeypatch, tombstones={"memory-a"})
    validated = []
    event = state_event(conflict_resolution={"base_key": "base-memory"})
    monkeypatch.setattr(
        backend,
        "_validate_resolution_event",
        lambda current, resolution: validated.append((current, resolution)),
    )

    assert backend.apply_sync_event(event) == "skipped"
    assert appended == [event]
    assert validated == []


def test_apply_sync_event_skips_older_state(monkeypatch):
    backend, memories, appended, *_ = make_apply_backend(
        monkeypatch, memories={"memory-a": memory_row(state_revision=5)}
    )
    event = state_event()

    assert backend.apply_sync_event(event) == "skipped"
    assert memories["memory-a"]["status"] == "active"
    assert appended == [event]


def test_apply_sync_event_deletes_memory_and_remembers_tombstone(monkeypatch):
    backend, memories, appended, _, deleted_orphans, *_ = make_apply_backend(
        monkeypatch, memories={"memory-a": memory_row()}
    )
    event = {
        "event_id": "origin-b:3",
        "kind": "tombstone",
        "key": "memory-a",
    }

    assert backend.apply_sync_event(event) == "updated"
    assert "memory-a" not in memories
    assert appended == [event]
    assert deleted_orphans == [True]


def test_apply_sync_event_records_tombstone_for_missing_memory(monkeypatch):
    backend, memories, appended, *_ = make_apply_backend(monkeypatch)
    event = {
        "event_id": "origin-b:3",
        "kind": "tombstone",
        "key": "memory-a",
    }

    assert backend.apply_sync_event(event) == "skipped"
    assert memories == {}
    assert appended == [event]
