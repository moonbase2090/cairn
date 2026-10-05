"""Authorization rules for importing Cairn's shared-vault event packs."""
from __future__ import annotations


def _resolution_matches_agent(resolution, agent_id: str, curator: bool) -> bool:
    if resolution is None or curator:
        return True
    return isinstance(resolution, dict) and resolution.get("resolved_by") == agent_id


def _snapshot_matches_agent(event: dict, agent_id: str, curator: bool, vault) -> bool:
    snapshot = event.get("snapshot")
    key = event.get("key")
    if not isinstance(snapshot, dict):
        return False
    owner = snapshot.get("agent_id")
    if (not isinstance(owner, str) or not isinstance(key, str)
            or not key.startswith(f"mem_{owner}_")):
        return False

    existing = vault.get(key)
    if existing is None:
        if owner != agent_id:
            return False
        parent = snapshot.get("supersedes")
        parent_row = vault.get(parent) if isinstance(parent, str) else None
        return not parent_row or parent_row["agent_id"] == agent_id or curator

    if existing["agent_id"] == agent_id or curator:
        return True
    return all(
        existing[field] == snapshot.get(field)
        for field in ("content_hash", "status", "archived_at", "expires_at")
    )


def _state_matches_agent(event: dict, agent_id: str, curator: bool, vault) -> bool:
    key = event.get("key")
    existing = vault.get(key) if isinstance(key, str) else None
    owner = existing["agent_id"] if existing is not None else event.get("agent_id")
    return curator or owner == agent_id


def _event_matches_agent(event: dict, agent_id: str, curator: bool, vault) -> bool:
    if not _resolution_matches_agent(event.get("conflict_resolution"), agent_id, curator):
        return False
    kind = event.get("kind")
    if kind == "snapshot":
        return _snapshot_matches_agent(event, agent_id, curator, vault)
    if kind in {"state", "tombstone"}:
        return _state_matches_agent(event, agent_id, curator, vault)
    return False


def sync_pack_matches_agent(pack: dict, agent_id: str, curator: bool, vault) -> bool:
    """Return whether a per-agent token may import each event in ``pack``."""
    events = pack.get("events") if isinstance(pack, dict) else None
    if not isinstance(events, list):
        return False
    for event in events:
        if not isinstance(event, dict):
            return False
        if vault.has_sync_event(str(event.get("event_id", ""))):
            continue  # An already accepted event may be relayed unchanged.
        if not _event_matches_agent(event, agent_id, curator, vault):
            return False
    return True
