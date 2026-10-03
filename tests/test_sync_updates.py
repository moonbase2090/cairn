"""Regression coverage for syncing changes to existing memory rows."""
from __future__ import annotations

import pytest

from cairn.client import CairnClient
from cairn.embed import HashEmbedder
from cairn.store import Vault


def make_client(db_path, agent_id: str) -> CairnClient:
    embedder = HashEmbedder()
    vault = Vault(db_path, embedder.name, embedder.dims, create=True)
    return CairnClient(vault, agent_id, embedder)


@pytest.mark.parametrize(
    ("change", "expected_status"),
    [("correct", "superseded"), ("archive", "archived")],
)
def test_sync_propagates_existing_row_status_changes(tmp_path, change, expected_status):
    source = make_client(tmp_path / "source" / "vault.db", "source-agent")
    peer = make_client(tmp_path / "peer" / "vault.db", "peer-agent")

    original = source.store_memory(
        "The staging service uses port 8100.",
        team_id="cairn",
        task_id="sync",
    )
    initial = source.export_delta()
    first = peer.import_sync_pack(initial)
    assert first["added"] == 1
    assert peer.get_memory(original.key).status == "active"

    if change == "correct":
        source.store_memory(
            "The staging service uses port 8101.",
            team_id="cairn",
            task_id="sync",
            supersedes_key=original.key,
            mode="new",
        )
    else:
        source.archive_memory(original.key)

    source_row = source.get_memory(original.key)
    assert source_row.status == expected_status

    peer.import_sync_pack(source.export_delta(initial["cursor"]))

    peer_row = peer.get_memory(original.key)
    assert peer_row.status == expected_status


def test_v1_packs_remain_insert_only_for_existing_rows(tmp_path):
    source = make_client(tmp_path / "source" / "vault.db", "source-agent")
    peer = make_client(tmp_path / "peer" / "vault.db", "peer-agent")
    original = source.store_memory("This fact will be archived.", team_id="cairn", task_id="sync")
    assert peer.import_pack(source.export()) == {"added": 1, "skipped": 0}

    source.archive_memory(original.key)
    assert peer.import_pack(source.export()) == {"added": 0, "skipped": 1}
    assert peer.get_memory(original.key).status == "active"


def test_state_revision_wins_and_delta_cursor_resumes(tmp_path):
    source = make_client(tmp_path / "source" / "vault.db", "source-agent")
    peer = make_client(tmp_path / "peer" / "vault.db", "peer-agent")
    stored = source.store_memory("Newest state wins.", team_id="cairn", task_id="sync")
    first = source.export_delta()
    peer.import_sync_pack(first, peer="server-a", direction="pull")
    assert peer.vault.get_sync_cursor("server-a", "pull") == first["cursor"]

    before = source.vault.get(stored.key)
    source.archive_memory(stored.key)
    archived = source.vault.get(stored.key)
    assert archived["state_revision"] == before["state_revision"] + 1
    assert archived["state_event_id"] != before["state_event_id"]

    delta = source.export_delta(first["cursor"])
    result = peer.import_sync_pack(delta, peer="server-a", direction="pull")
    assert result["updated"] == 1
    assert peer.get_memory(stored.key).status == "archived"
    repeated = peer.import_sync_pack(delta, peer="server-a", direction="pull")
    assert repeated["skipped"] == 1
    assert peer.vault.get_sync_cursor("server-a", "pull") == delta["cursor"]


def test_sync_import_rejects_a_cursor_gap_without_moving_the_saved_cursor(tmp_path):
    source = make_client(tmp_path / "source" / "vault.db", "source-agent")
    peer = make_client(tmp_path / "peer" / "vault.db", "peer-agent")
    source.store_memory("The peer cursor must not jump.", team_id="cairn", task_id="sync")
    first = source.export_delta()
    peer.import_sync_pack(first, peer="server-a", direction="pull")
    assert peer.vault.get_sync_cursor("server-a", "pull") == first["cursor"]

    gap = source.export_delta(first["cursor"] + 1)
    with pytest.raises(ValueError, match="starts after the saved peer cursor"):
        peer.import_sync_pack(gap, peer="server-a", direction="pull")
    assert peer.vault.get_sync_cursor("server-a", "pull") == first["cursor"]


def test_higher_state_revision_beats_a_later_timestamp(tmp_path):
    source = make_client(tmp_path / "source" / "vault.db", "source-agent")
    peer = make_client(tmp_path / "peer" / "vault.db", "peer-agent")
    stored = source.store_memory("The revision decides the winner.", team_id="cairn", task_id="sync")
    peer.import_sync_pack(source.export_delta())
    row = peer.vault.get(stored.key)
    base_revision = row["state_revision"]

    newer = {
        "event_id": "origin-new:2", "origin_id": "origin-new", "origin_seq": 2,
        "kind": "state", "key": stored.key,
        "state_revision": base_revision + 2, "updated_at": 1,
        "state_origin": "origin-new", "state_event_id": "origin-new:2",
        "status": "archived", "archived_at": 1,
    }
    older = {
        "event_id": "origin-old:99", "origin_id": "origin-old", "origin_seq": 99,
        "kind": "state", "key": stored.key,
        "state_revision": base_revision + 1, "updated_at": 4_000_000_000,
        "state_origin": "origin-old", "state_event_id": "origin-old:99",
        "status": "active", "archived_at": None,
    }
    assert peer.vault.apply_sync_event(newer) == "updated"
    assert peer.vault.apply_sync_event(older) == "skipped"
    assert peer.get_memory(stored.key).status == "archived"


def test_tombstones_propagate_and_are_retained_in_the_event_log(tmp_path):
    source = make_client(tmp_path / "source" / "vault.db", "source-agent")
    peer = make_client(tmp_path / "peer" / "vault.db", "peer-agent")
    stored = source.store_memory("This fact is hard deleted.", team_id="cairn", task_id="sync")
    legacy_snapshot = source.export()
    first = source.export_delta()
    peer.import_sync_pack(first)
    late_peer = make_client(tmp_path / "late-peer" / "vault.db", "late-peer")
    late_peer.import_sync_pack(first)

    source.archive_memory(stored.key)
    archived = source.export_delta(first["cursor"])
    state_event = archived["events"][0]
    source.purge_memory(stored.canonical_id)
    deletion = source.export_delta(first["cursor"])
    tombstone = next(event for event in deletion["events"] if event["kind"] == "tombstone")
    peer.import_sync_pack(deletion)
    assert peer.get_memory(stored.key) is None
    assert peer.import_pack(legacy_snapshot) == {"added": 0, "skipped": 1}
    assert any(event["kind"] == "tombstone"
               for event in peer.export_delta()["events"])

    tombstone_only = {**deletion, "after": tombstone["feed_seq"] - 1,
                      "cursor": tombstone["feed_seq"], "events": [tombstone]}
    late_peer.import_sync_pack(tombstone_only)
    assert late_peer.vault.apply_sync_event(state_event) == "skipped"
    assert late_peer.get_memory(stored.key) is None


def test_competing_corrections_are_recorded_resolved_and_synced(tmp_path):
    agent_a = make_client(tmp_path / "a" / "vault.db", "agent-a")
    agent_b = make_client(tmp_path / "b" / "vault.db", "agent-b")
    agent_c = make_client(tmp_path / "c" / "vault.db", "agent-c")
    merged = make_client(tmp_path / "merged" / "vault.db", "agent-a")
    peer = make_client(tmp_path / "peer" / "vault.db", "peer-agent")
    root = agent_a.store_memory("The endpoint is /v1.", team_id="cairn", task_id="sync")
    agent_b.import_pack(agent_a.export())
    agent_c.import_pack(agent_a.export())

    correction_a = agent_a.store_memory(
        "The endpoint is /v2.", team_id="cairn", task_id="sync",
        supersedes_key=root.key, mode="new",
    )
    correction_b = agent_b.store_memory(
        "The endpoint is /v3.", team_id="cairn", task_id="sync",
        supersedes_key=root.key, mode="new",
    )
    correction_c = agent_c.store_memory(
        "The endpoint is /v4.", team_id="cairn", task_id="sync",
        supersedes_key=root.key, mode="new",
    )
    merged.import_sync_pack(agent_a.export_delta())
    merged.import_sync_pack(agent_b.export_delta())
    merged.import_sync_pack(agent_c.export_delta())

    conflicts = merged.sync_status()["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["base_key"] == root.key
    assert set(conflicts[0]["competitor_keys"]) == {
        correction_a.key, correction_b.key, correction_c.key,
    }
    assert merged.get_memory(correction_a.key).status == "active"
    assert merged.get_memory(correction_b.key).status == "active"
    assert merged.get_memory(correction_c.key).status == "active"

    # An owner cannot change another agent's correction; a curator can.
    with pytest.raises(PermissionError, match="curator"):
        merged.resolve_competing_correction(root.key, correction_a.key)
    resolved = merged.resolve_competing_correction(root.key, correction_a.key, curator=True)
    assert set(resolved["superseded"]) == {correction_b.key, correction_c.key}
    assert merged.get_memory(correction_b.key).status == "superseded"
    assert merged.get_memory(correction_c.key).status == "superseded"
    assert merged.sync_status()["conflicts"] == []

    peer.import_sync_pack(merged.export_delta())
    assert peer.get_memory(correction_b.key).status == "superseded"
    assert peer.get_memory(correction_c.key).status == "superseded"
    assert peer.sync_status()["conflicts"] == []
