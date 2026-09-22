"""Hybrid vault storage tests — threshold spill, refcounts, integrity, migration."""
import json
import sqlite3

import pytest

from cairn.client import CairnClient
from cairn.embed import HashEmbedder
from cairn.store import ContentIntegrityError, Vault

BIG = "# Runbook\n\n" + "Step `Restart` the **flapjack** array. " * 60  # mixed-case markdown


def make_client(db_path, agent="doc-bot", threshold=100):
    emb = HashEmbedder()
    return CairnClient(Vault(db_path, emb.name, emb.dims, create=True, doc_threshold=threshold),
                       agent, emb)


def test_small_stays_inline(tmp_path):
    c = make_client(tmp_path / "v.db")
    r = c.store_memory("tiny", team_id="t", task_id="k")
    row = c.vault.get(r.key)
    assert row["content"] == "tiny" and row["content_ref"] is None
    assert c.stats()["docs"] == {"files": 0, "bytes": 0}


def test_large_spills_with_byte_fidelity(tmp_path):
    c = make_client(tmp_path / "v.db")
    r = c.store_memory(BIG, team_id="t", task_id="k")
    row = c.vault.get(r.key)
    assert row["content"] is None and row["content_ref"]
    p = c.vault.doc_path(row["content_ref"])
    assert p.exists() and ":" not in p.name  # bare hex, portable filename
    assert c.get_memory(r.key).content == BIG  # case + markdown intact


def test_boundary_is_inline(tmp_path):
    c = make_client(tmp_path / "v.db", threshold=10)
    r = c.store_memory("1234567890", team_id="t", task_id="k")  # exactly 10 bytes
    assert c.vault.get(r.key)["content"] == "1234567890"


def test_reads_resolve_everywhere(tmp_path):
    c = make_client(tmp_path / "v.db")
    r = c.store_memory(BIG + " zebra", team_id="t", task_id="k")
    assert c.retrieve_memory("zebra")[0].content == c.get_memory(r.key).content == BIG + " zebra"
    assert c.list_memories({"search": "zebra"})[0].content.endswith("zebra")
    from cairn.galaxy import to_points

    pts = to_points(c)
    assert pts and pts[0]["text"].startswith("# Runbook")
    assert "full" not in pts[0]
    pack = c.export()["memories"][0]
    assert pack["content"] == BIG + " zebra" and "content_ref" not in pack


def test_shared_content_single_file_refcounted(tmp_path):
    c = make_client(tmp_path / "v.db")
    a = c.store_memory(BIG, team_id="t", task_id="k1")
    b = c.store_memory(BIG, team_id="t", task_id="k2")
    p = c.vault.doc_path(c.vault.get(a.key)["content_ref"])
    assert c.stats()["docs"]["files"] == 1
    c.vault.delete_by_keys([a.key])
    assert p.exists()  # b still references it
    assert c.get_memory(b.key).content == BIG
    c.vault.delete_by_keys([b.key])
    assert not p.exists()


def test_gc_keeps_live_spilled_docs(tmp_path):
    c = make_client(tmp_path / "v.db")
    r = c.store_memory(BIG, team_id="t", task_id="k")
    p = c.vault.doc_path(c.vault.get(r.key)["content_ref"])
    assert p.exists()
    assert c.gc(dry_run=False)["orphan_docs"] == 0
    assert p.exists()
    assert c.get_memory(r.key).content == BIG


def test_gc_sweeps_stray_docs(tmp_path):
    c = make_client(tmp_path / "v.db")
    stray = c.vault.docs_root / "ab" / "cd" / ("ab" + "cd" + "e" * 60 + ".md")
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("stray")
    assert c.gc(dry_run=False)["orphan_docs"] == 1
    assert not stray.exists()


def test_corrupt_and_missing_docs_are_loud(tmp_path):
    c = make_client(tmp_path / "v.db")
    r = c.store_memory(BIG, team_id="t", task_id="k")
    p = c.vault.doc_path(c.vault.get(r.key)["content_ref"])
    p.write_text("tampered!")
    with pytest.raises(ContentIntegrityError):
        c.get_memory(r.key)
    p.unlink()
    with pytest.raises(ContentIntegrityError):
        c.get_memory(r.key)


def test_export_import_roundtrip_preserves_big_docs(tmp_path):
    c = make_client(tmp_path / "v.db")
    c.store_memory(BIG, team_id="t", task_id="k")
    c.store_memory("small", team_id="t", task_id="k")
    pack = c.export()
    c2 = make_client(tmp_path / "v2.db")
    assert c2.import_pack(pack) == {"added": 2, "skipped": 0}
    assert c2.import_pack(pack) == {"added": 0, "skipped": 2}  # idempotent
    got = c2.list_memories({"task_id": "k"})
    assert sorted(m.content for m in got) == sorted(["small", BIG])


def test_old_pack_without_content_ref_imports(tmp_path):
    c = make_client(tmp_path / "v.db")
    c.store_memory(BIG, team_id="t", task_id="k")
    pack = c.export()
    for m in pack["memories"]:
        del m["content"]  # simulate a v1 pack shape... no — v1 packs HAVE content
    pack["memories"][0]["content"] = BIG  # restore: v1 packs carry full content, no ref
    c2 = make_client(tmp_path / "v2.db")
    assert c2.import_pack(pack)["added"] == 1
    assert c2.get_memory(pack["memories"][0]["key"]).content == BIG


def _build_v1_db(path):
    """Hand-roll a schema-1 vault: NOT NULL content, no content_ref, old triggers."""
    emb = HashEmbedder()
    con = sqlite3.connect(str(path))
    con.executescript(f"""
        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
        CREATE TABLE memories(
          rowid INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, canonical_id TEXT NOT NULL,
          content TEXT NOT NULL, content_summary TEXT, memory_type TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active', origin TEXT NOT NULL DEFAULT 'agent',
          task_id TEXT NOT NULL, agent_id TEXT NOT NULL, team_id TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL, expires_at INTEGER, archived_at INTEGER,
          supersedes TEXT, parent_key TEXT, provenance TEXT, confidence REAL,
          content_hash TEXT NOT NULL, embedding BLOB NOT NULL);
        CREATE INDEX idx_mem_task ON memories(task_id, created_at);
        CREATE VIRTUAL TABLE mem_fts USING fts5(key UNINDEXED, content, tokenize='unicode61');
        CREATE TRIGGER mem_fts_ai AFTER INSERT ON memories BEGIN
          INSERT INTO mem_fts(rowid, key, content) VALUES (new.rowid, new.key, new.content); END;
        CREATE TRIGGER mem_fts_ad AFTER DELETE ON memories BEGIN
          DELETE FROM mem_fts WHERE rowid = old.rowid; END;
        CREATE TRIGGER mem_fts_au AFTER UPDATE OF content ON memories BEGIN
          DELETE FROM mem_fts WHERE rowid = old.rowid;
          INSERT INTO mem_fts(rowid, key, content) VALUES (new.rowid, new.key, new.content); END;
        INSERT INTO meta(k, v) VALUES('embed_model', '{emb.name}'), ('dims', '{emb.dims}'), ('schema', '1');
    """)
    import numpy as np

    blob = np.zeros(emb.dims, dtype=np.float32).tobytes()
    con.execute(
        "INSERT INTO memories(key, canonical_id, content, memory_type, task_id, agent_id,"
        " team_id, created_at, updated_at, content_hash, embedding)"
        " VALUES('k1', 'c1', 'legacy hello world', 'semantic', 't', 'a', 'team', 1, 1, 'sha256:x', ?)",
        (blob,))
    con.commit()
    con.close()


def test_v1_migrates_with_backup_and_triggers(tmp_path):
    db = tmp_path / "legacy.db"
    _build_v1_db(db)
    emb = HashEmbedder()
    v = Vault(db, emb.name, emb.dims)  # open migrates
    assert (tmp_path / "legacy.db.pre2.bak").exists()
    assert v._get_meta("schema") == "2"
    row = v.get("k1")
    assert row["content"] == "legacy hello world" and row["content_ref"] is None
    trigs = {r["sql"] for r in v.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger'").fetchall()}
    assert trigs and all("WHEN NEW.content IS NOT NULL" in (s or "") for s in trigs
                         if "mem_fts_ai" in (s or "") or "mem_fts_au" in (s or ""))
    assert v.fts_search("legacy")[0]["key"] == "k1"  # old rows stay searchable
    v.close()
    # reopen is a clean no-op; new big rows spill, small rows index via trigger
    c = CairnClient(Vault(db, emb.name, emb.dims), "a", emb)
    c.store_memory("fresh small legacy searchterm", team_id="t", task_id="k")
    assert c.list_memories({"search": "legacy searchterm"})
    big = c.store_memory(BIG, team_id="t", task_id="k2")
    assert c.vault.get(big.key)["content"] is None
    assert c.list_memories({"search": "flapjack"})[0].content == BIG


def test_init_doc_threshold_and_doctor(tmp_path, monkeypatch, capsys):
    from cairn.cli import main

    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "doc-test")
    assert main(["init", "--json", "--doc-threshold", "10"]) == 0
    capsys.readouterr()
    assert main(["--json", "doctor"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["doc_threshold"] == 10 and doc["docs"] == {"files": 0, "bytes": 0}
