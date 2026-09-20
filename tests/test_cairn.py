"""Scaffold tests — all offline (HashEmbedder, tmp vaults)."""
import base64
import time

import numpy as np
import pytest

from cairn.client import CairnClient
from cairn.embed import HashEmbedder
from cairn.store import SpaceMismatchError, Vault


def make_client(tmp_path, agent="claude-cairn"):
    emb = HashEmbedder()
    vault = Vault(tmp_path / "vault.db", emb.name, emb.dims, create=True)
    return CairnClient(vault, agent, emb), vault, emb


def test_store_retrieve_roundtrip(tmp_path):
    client, _, _ = make_client(tmp_path)
    res = client.store_memory("Q2 revenue grew 12 percent year over year", team_id="acme", task_id="q2")
    assert res.action.value == "created"
    hits = client.retrieve_memory("how did Q2 revenue do?", filters={"task_id": "q2"})
    assert hits, "retrieve found nothing"
    assert hits[0].key == res.key
    # hash embedder is coarse (exact=1.0, unrelated≈0) — related pair must rank first, well above noise
    assert hits[0].similarity and hits[0].similarity > 0.2


def test_exact_duplicate_is_noop(tmp_path):
    client, _, _ = make_client(tmp_path)
    a = client.store_memory("Benchmark providers on price per kilogram", team_id="t", task_id="k")
    b = client.store_memory("Benchmark providers on price per kilogram", team_id="t", task_id="k")
    assert b.action.value == "unchanged"
    assert b.key == a.key


def test_near_duplicate_supersede_collapse(tmp_path):
    client, _, _ = make_client(tmp_path)
    a = client.store_memory("the quick brown fox jumps over the lazy dog", team_id="t", task_id="k")
    b = client.store_memory("the quick brown fox jumps over the lazy dogs", team_id="t", task_id="k")
    assert b.action.value == "duplicate_detected", f"got {b.action} — adjust fixture strings"
    assert b.near_duplicates, "expected near-duplicate candidates"
    # explicit new fact bypasses the screen
    c = client.store_memory("the quick brown fox jumps over the lazy dogs", team_id="t", task_id="k", mode="new")
    assert c.action.value == "created"
    # correction supersedes and collapses to v2 on read
    d = client.store_memory("the quick brown fox jumps over the energetic dog", team_id="t", task_id="k",
                            supersedes_key=a.key, mode="new")
    assert d.action.value == "superseded"
    assert d.version == 2
    hits = client.retrieve_memory("quick brown fox", filters={"task_id": "k"}, top_k=10)
    keys = [h.key for h in hits]
    assert d.key in keys and a.key not in keys


def test_knn_skips_archived_and_returns_rows(tmp_path):
    client, vault, _ = make_client(tmp_path)
    live = client.store_memory("live coolant manifold reading one", team_id="t", task_id="k")
    dead = client.store_memory("archived coolant manifold reading two", team_id="t", task_id="k", mode="new")
    client.archive_memory(dead.key)
    hits = vault.knn(client._embed_one("coolant manifold reading"), k=10)
    keys = [row["key"] for row, _dist in hits]
    assert live.key in keys
    assert dead.key not in keys
    assert all(row["status"] == "active" for row, _ in hits)


def test_archive_restore(tmp_path):
    client, _, _ = make_client(tmp_path)
    res = client.store_memory("ephemeral scratch note for tests", team_id="t", task_id="k")
    client.archive_memory(res.key)
    assert res.key not in [h.key for h in client.retrieve_memory("scratch note", filters={"task_id": "k"})]
    client.restore_memory(res.key)
    assert res.key in [h.key for h in client.retrieve_memory("scratch note", filters={"task_id": "k"})]


def test_purge(tmp_path):
    client, _, _ = make_client(tmp_path)
    res = client.store_memory("to be forgotten entirely", team_id="t", task_id="k")
    out = client.purge_memory(res.canonical_id)
    assert out["deleted"] >= 1
    assert client.get_memory(res.key) is None


def test_gc_promotes_stale_superseded(tmp_path):
    client, vault, _ = make_client(tmp_path)
    a = client.store_memory("original claim here today", team_id="t", task_id="k")
    client.store_memory("corrected claim here today now", team_id="t", task_id="k",
                        supersedes_key=a.key, mode="new")
    old = int(time.time()) - 8 * 86400
    vault.conn.execute("UPDATE memories SET updated_at=? WHERE key=?", (old, a.key))
    vault.conn.commit()
    dry = client.gc(dry_run=True)
    assert dry["promoted"] == 1 and dry["dry_run"] is True
    real = client.gc(dry_run=False)
    assert real.get("deleted", 0) == 0
    assert client.get_memory(a.key).status == "archived"


def test_export_import_roundtrip(tmp_path):
    c1, _, emb = make_client(tmp_path / "a")
    c1.store_memory("shared team fact alpha beta gamma", team_id="t", task_id="k")
    pack = c1.export()
    assert pack["embed_model"] == emb.name
    raw = pack["memories"][0]["embedding"]
    assert isinstance(raw, str)
    vec = np.frombuffer(base64.b64decode(raw), dtype=np.float32)
    assert vec.size == emb.dims
    emb2 = HashEmbedder()
    v2 = Vault(tmp_path / "b" / "vault.db", emb2.name, emb2.dims, create=True)
    c2 = CairnClient(v2, "grok-cairn", emb2)
    out = c2.import_pack(pack)
    assert out == {"added": 1, "skipped": 0}
    out2 = c2.import_pack(pack)  # idempotent
    assert out2 == {"added": 0, "skipped": 1}
    assert c2.retrieve_memory("team fact", filters={"task_id": "k"})
    # legacy JSON float lists still import
    legacy_row = dict(pack["memories"][0])
    legacy_row["key"] = pack["memories"][0]["key"] + "-legacy"
    legacy_row["embedding"] = vec.tolist()
    assert c2.import_pack({**pack, "memories": [legacy_row]}) == {"added": 1, "skipped": 0}


def test_vault_uses_wal_and_mmap(tmp_path):
    emb = HashEmbedder()
    vault = Vault(tmp_path / "vault.db", emb.name, emb.dims, create=True)
    mode = vault.conn.execute("PRAGMA journal_mode").fetchone()[0]
    mmap = vault.conn.execute("PRAGMA mmap_size").fetchone()[0]
    cache = vault.conn.execute("PRAGMA cache_size").fetchone()[0]
    vault.close()
    assert mode.lower() == "wal"
    assert mmap >= 268435456
    assert cache == -8000


def test_space_mismatch_refused(tmp_path):
    emb = HashEmbedder()
    Vault(tmp_path / "vault.db", emb.name, emb.dims, create=True).close()
    other = HashEmbedder(dims=128)
    with pytest.raises(SpaceMismatchError):
        Vault(tmp_path / "vault.db", other.name, other.dims)
