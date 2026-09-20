"""BM25 keyword search (FTS5) tests — offline, tmp dirs."""
import json

from cairn.cli import main
from cairn.client import CairnClient
from cairn.embed import HashEmbedder
from cairn.store import Vault


def make_client(db_path, agent="bm25-bot"):
    emb = HashEmbedder()
    return CairnClient(Vault(db_path, emb.name, emb.dims, create=True), agent, emb)


def seed(client):
    client.store_memory("the cairn-embedd daemon listens on a unix socket", team_id="t", task_id="k")
    client.store_memory("socket permissions: the socket path must be 0700 in the runtime dir",
                        team_id="t", task_id="k")
    client.store_memory("unrelated note about coolant manifolds", team_id="t", task_id="k")


def test_search_finds_literal_terms_best_first(tmp_path):
    client = make_client(tmp_path / "vault.db")
    seed(client)
    # AND semantics: only the doc with both terms
    hits = client.list_memories({"search": "unix socket"})
    assert [h.content for h in hits] == ["the cairn-embedd daemon listens on a unix socket"]
    # higher term frequency outranks ("socket" x2 beats "socket" x1)
    hits = client.list_memories({"search": "socket"})
    assert [h.content for h in hits] == [
        "socket permissions: the socket path must be 0700 in the runtime dir",
        "the cairn-embedd daemon listens on a unix socket",
    ]
    assert all("manifold" not in h.content for h in hits)


def test_search_literal_dots_and_syntax_are_safe(tmp_path):
    client = make_client(tmp_path / "vault.db")
    seed(client)
    assert len(client.list_memories({"search": "cairn-embedd"})) == 1  # dot is literal
    assert client.list_memories({"search": '" * ('}) == []  # no FTS syntax error
    assert client.list_memories({"search": "   "}) == []  # blank is clean []


def test_search_respects_status_and_expiry(tmp_path):
    client = make_client(tmp_path / "vault.db")
    seed(client)
    key = client.list_memories({"search": "manifolds"})[0].key
    client.archive_memory(key)
    assert client.list_memories({"search": "manifolds"}) == []
    assert len(client.list_memories({"search": "manifolds", "status": "archived"})) == 1
    # expired rows never surface
    exp = client.store_memory("expiring note about zebra migration", team_id="t", task_id="k")
    assert len(client.list_memories({"search": "zebra"})) == 1
    client.vault.conn.execute("UPDATE memories SET expires_at=1 WHERE key=?", (exp.key,))
    client.vault.conn.commit()
    assert client.list_memories({"search": "zebra"}) == []


def test_search_combines_with_exact_filters(tmp_path):
    client = make_client(tmp_path / "vault.db")
    seed(client)
    client.store_memory("socket note for another task", team_id="t", task_id="other")
    hits = client.list_memories({"search": "socket", "task_id": "other"})
    assert len(hits) == 1 and hits[0].task_id == "other"


def test_purge_cleans_the_index(tmp_path):
    client = make_client(tmp_path / "vault.db")
    seed(client)
    canon = client.list_memories({"search": "manifolds"})[0].canonical_id
    client.purge_memory(canon)
    assert client.list_memories({"search": "manifolds"}) == []


def test_legacy_vault_backfills_on_open(tmp_path):
    db = tmp_path / "vault.db"
    client = make_client(db)
    seed(client)
    client.vault.conn.execute("DELETE FROM mem_fts")  # simulate a pre-FTS vault
    client.vault.conn.commit()
    emb = HashEmbedder()
    reopened = CairnClient(Vault(db, emb.name, emb.dims), "bm25-bot", emb)
    assert len(reopened.list_memories({"search": "socket"})) == 2


def test_cli_list_search(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "cli-test")
    assert main(["init", "--yes", "--embed-spec", "hash"]) == 0
    capsys.readouterr()
    assert main(["store", "the token lives in the keychain", "--team", "t", "--task", "k"]) == 0
    capsys.readouterr()
    assert main(["list", "--search", "keychain", "--json"]) == 0
    out, _ = capsys.readouterr()
    hits = json.loads(out)
    assert len(hits) == 1 and "keychain" in hits[0]["content"]
    assert main(["list", "--json"]) == 2  # still needs a filter without --search


def test_mcp_list_search(tmp_path, monkeypatch):
    from cairn import mcp_server
    from cairn.cli import main

    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "mcp-test")
    assert main(["init", "--yes", "--embed-spec", "hash"]) == 0
    client = mcp_server.make_client()
    client.store_memory("the gateway token rotates hourly", team_id="t", task_id="k")
    out = mcp_server.call_tool(client, "list_memories", {"search": "gateway"})
    assert "gateway" in out
    try:
        mcp_server.call_tool(client, "list_memories", {})
    except ValueError as e:
        assert "search" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
