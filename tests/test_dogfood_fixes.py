"""Dogfood-fix tests — from the grok-cairn-fresh 0.2.0 session report."""
import json

import pytest

from cairn.cli import main
from cairn.client import CairnClient
from cairn.embed import HashEmbedder
from cairn.store import Vault


def make_client(db_path, agent="dogfood"):
    emb = HashEmbedder()
    return CairnClient(Vault(db_path, emb.name, emb.dims, create=True), agent, emb)


def run(argv, capsys):
    rc = main(argv)
    return rc, *capsys.readouterr()


def test_store_rejects_empty_content(tmp_path):
    client = make_client(tmp_path / "vault.db")
    with pytest.raises(ValueError, match="non-empty"):
        client.store_memory("   ", team_id="t", task_id="k")
    with pytest.raises(ValueError, match="non-empty"):
        client.store_memory("", team_id="t", task_id="k")


def test_cli_store_empty_is_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "cli-empty")
    assert run(["init", "--yes", "--embed-spec", "hash"], capsys)[0] == 0
    rc, _, err = run(["store", "   ", "--team", "t", "--task", "k"], capsys)
    assert rc == 2 and "non-empty" in err


def test_restore_superseded_retires_rival(tmp_path):
    client = make_client(tmp_path / "vault.db")
    v1 = client.store_memory("original claim here today", team_id="t", task_id="k")
    v2 = client.store_memory("corrected claim here today now", team_id="t", task_id="k",
                             supersedes_key=v1.key, mode="new")
    out = client.restore_memory(v1.key)
    assert out == {"key": v1.key, "status": "active", "retired": [v2.key]}
    assert client.get_memory(v1.key).status == "active"
    assert client.get_memory(v2.key).status == "archived"
    hits = client.retrieve_memory("claim here", filters={"task_id": "k"}, top_k=10)
    assert [h.key for h in hits] == [v1.key]


def test_restore_archived_just_reactivates(tmp_path):
    client = make_client(tmp_path / "vault.db")
    res = client.store_memory("ephemeral note here", team_id="t", task_id="k")
    client.archive_memory(res.key)
    out = client.restore_memory(res.key)
    assert out == {"key": res.key, "status": "active", "retired": []}


def test_retrieve_min_sim_floor(tmp_path):
    client = make_client(tmp_path / "vault.db")
    client.store_memory("coolant manifold pressure nominal levels", team_id="t", task_id="k")
    assert client.retrieve_memory("totally unrelated zebra query")  # legacy: always top-k
    assert client.retrieve_memory("totally unrelated zebra query", min_similarity=0.9) == []
    hits = client.retrieve_memory("coolant manifold pressure", min_similarity=0.2)
    assert hits


def test_import_garbage_pack_errors_cleanly(tmp_path):
    client = make_client(tmp_path / "vault.db")
    with pytest.raises(ValueError, match="invalid pack"):
        client.import_pack({})
    with pytest.raises(ValueError, match="invalid pack"):
        client.import_pack([])
    with pytest.raises(ValueError, match="invalid pack"):
        client.import_pack({"memories": []})


def test_human_log_renders_audit_rows(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "log-check")
    assert run(["init", "--yes", "--embed-spec", "hash"], capsys)[0] == 0
    assert run(["store", "logged memory one two", "--team", "t", "--task", "k"], capsys)[0] == 0
    pack = json.loads(run(["export", "--json"], capsys)[1])
    (tmp_path / "pack.json").write_text(json.dumps(pack))
    assert run(["import", str(tmp_path / "pack.json")], capsys)[0] == 0
    rc, out, _ = run(["log"], capsys)
    assert rc == 0
    assert "[None/None]" not in out and "None" not in out
    assert "store" in out and "import" in out
