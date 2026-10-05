from __future__ import annotations

import pytest

from cairn.sync_auth import sync_pack_matches_agent


class FakeVault:
    def __init__(self, rows=None, accepted=None):
        self.rows = rows or {}
        self.accepted = accepted or set()

    def has_sync_event(self, event_id):
        return event_id in self.accepted

    def get(self, key):
        return self.rows.get(key)


def _snapshot_event(*, key="mem_agent-a_task_1", owner="agent-a", **values):
    return {
        "event_id": "event-1", "kind": "snapshot", "key": key,
        "snapshot": {"agent_id": owner, **values},
    }


def test_pack_shape_and_non_object_events_are_rejected():
    vault = FakeVault()

    assert not sync_pack_matches_agent(None, "agent-a", False, vault)
    assert not sync_pack_matches_agent({"events": None}, "agent-a", False, vault)
    assert not sync_pack_matches_agent({"events": [None]}, "agent-a", False, vault)


def test_already_accepted_event_can_be_relayed_without_rechecking_ownership():
    event = {"event_id": "accepted", "kind": "invalid"}

    assert sync_pack_matches_agent(
        {"events": [event]}, "agent-a", False, FakeVault(accepted={"accepted"}),
    )


@pytest.mark.parametrize("resolution", ["invalid", {}, {"resolved_by": "agent-b"}])
def test_non_curator_cannot_import_another_agents_conflict_resolution(resolution):
    event = {**_snapshot_event(), "conflict_resolution": resolution}

    assert not sync_pack_matches_agent({"events": [event]}, "agent-a", False, FakeVault())


def test_conflict_resolution_requires_the_assigned_agent_unless_curator():
    event = {**_snapshot_event(), "conflict_resolution": {"resolved_by": "agent-a"}}
    denied = {**_snapshot_event(), "conflict_resolution": {"resolved_by": "agent-b"}}

    assert sync_pack_matches_agent({"events": [event]}, "agent-a", False, FakeVault())
    assert sync_pack_matches_agent({"events": [denied]}, "agent-a", True, FakeVault())


@pytest.mark.parametrize(
    "event",
    [
        _snapshot_event(key="mem_agent-b_task_1"),
        _snapshot_event(owner=42),
        {"event_id": "event-1", "kind": "snapshot", "key": "mem_agent-a_task_1", "snapshot": None},
    ],
)
def test_snapshot_requires_a_well_formed_owner_and_matching_memory_key(event):
    assert not sync_pack_matches_agent({"events": [event]}, "agent-a", False, FakeVault())


def test_new_snapshot_must_belong_to_the_assigned_agent_and_have_no_foreign_parent():
    foreign = _snapshot_event(owner="agent-b", key="mem_agent-b_task_1")
    parent = _snapshot_event(supersedes="mem_agent-b_parent_1")
    vault = FakeVault(rows={"mem_agent-b_parent_1": {"agent_id": "agent-b"}})

    assert not sync_pack_matches_agent({"events": [foreign]}, "agent-a", False, FakeVault())
    assert not sync_pack_matches_agent({"events": [parent]}, "agent-a", False, vault)
    assert sync_pack_matches_agent({"events": [parent]}, "agent-a", True, vault)


def test_own_snapshot_can_supersede_own_memory():
    event = _snapshot_event(supersedes="mem_agent-a_parent_1")
    vault = FakeVault(rows={"mem_agent-a_parent_1": {"agent_id": "agent-a"}})

    assert sync_pack_matches_agent({"events": [event]}, "agent-a", False, vault)


def test_foreign_snapshot_may_only_echo_unchanged_state():
    key = "mem_agent-b_task_1"
    row = {
        "agent_id": "agent-b", "content_hash": "sha256:abc", "status": "active",
        "archived_at": None, "expires_at": None,
    }
    unchanged = _snapshot_event(
        key=key, owner="agent-b", content_hash="sha256:abc", status="active",
        archived_at=None, expires_at=None,
    )
    changed = _snapshot_event(key=key, owner="agent-b", content_hash="sha256:new")
    vault = FakeVault(rows={key: row})

    assert sync_pack_matches_agent({"events": [unchanged]}, "agent-a", False, vault)
    assert not sync_pack_matches_agent({"events": [changed]}, "agent-a", False, vault)
    assert sync_pack_matches_agent({"events": [changed]}, "agent-a", True, vault)


@pytest.mark.parametrize("kind", ["state", "tombstone"])
def test_state_and_tombstone_events_are_limited_to_their_owner(kind):
    event = {
        "event_id": "event-2", "kind": kind, "key": "mem_agent-a_task_1",
        "agent_id": "agent-a",
    }

    assert sync_pack_matches_agent({"events": [event]}, "agent-a", False, FakeVault())
    foreign = {**event, "agent_id": "agent-b"}
    assert not sync_pack_matches_agent({"events": [foreign]}, "agent-a", False, FakeVault())
    assert sync_pack_matches_agent({"events": [foreign]}, "agent-a", True, FakeVault())


def test_existing_foreign_state_uses_the_stored_owner_and_unknown_events_fail():
    state = {"event_id": "event-3", "kind": "state", "key": "mem_agent-b_task_1", "agent_id": "agent-a"}
    vault = FakeVault(rows={"mem_agent-b_task_1": {"agent_id": "agent-b"}})
    unknown = {"event_id": "event-4", "kind": "other", "key": "mem_agent-a_task_1"}

    assert not sync_pack_matches_agent({"events": [state]}, "agent-a", False, vault)
    assert not sync_pack_matches_agent({"events": [unknown]}, "agent-a", False, FakeVault())
