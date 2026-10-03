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
    assert peer.import_pack(source.export()) == {"added": 1, "skipped": 0}

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

    peer.import_pack(source.export())

    peer_row = peer.get_memory(original.key)
    assert peer_row.status == expected_status
