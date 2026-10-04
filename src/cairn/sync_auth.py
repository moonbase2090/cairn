"""Authorization rules for importing Cairn's shared-vault event packs."""
from __future__ import annotations


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
        kind, key = event.get("kind"), event.get("key")
        resolution = event.get("conflict_resolution")
        if (resolution is not None and not curator
                and (not isinstance(resolution, dict)
                     or resolution.get("resolved_by") != agent_id)):
            return False
        existing = vault.get(key) if isinstance(key, str) else None
        if kind == "snapshot":
            snapshot = event.get("snapshot")
            if not isinstance(snapshot, dict):
                return False
            owner = snapshot.get("agent_id")
            if not isinstance(owner, str) or not isinstance(key, str) or not key.startswith(f"mem_{owner}_"):
                return False
            if existing is None:
                if owner != agent_id:
                    return False
                parent = snapshot.get("supersedes")
                parent_row = vault.get(parent) if isinstance(parent, str) else None
                if parent_row and parent_row["agent_id"] != agent_id and not curator:
                    return False
            elif existing["agent_id"] != agent_id and not curator:
                # Pulled foreign rows may be echoed unchanged, but an agent
                # cannot advance another owner's state through a snapshot.
                if (existing["content_hash"] != snapshot.get("content_hash")
                        or existing["status"] != snapshot.get("status")
                        or existing["archived_at"] != snapshot.get("archived_at")
                        or existing["expires_at"] != snapshot.get("expires_at")):
                    return False
        elif kind in {"state", "tombstone"}:
            if existing is not None:
                owner = existing["agent_id"]
            else:
                owner = event.get("agent_id")
            if owner != agent_id and not curator:
                return False
        else:
            return False
    return True
