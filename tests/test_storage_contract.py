"""Behaviour every storage backend must provide. Uses only the StorageBackend API.

TestStorageContract runs once per name in CONTRACT_BACKENDS. A new backend
adds its name there (with a skip mark when its server is not configured) and
must pass unchanged. Nothing in the contract may touch a database driver.
"""
from __future__ import annotations

import os
import uuid

import numpy as np
import pytest

from cairn.models import content_digest
from cairn.storage import (
    BACKENDS,
    MEMORY_FIELDS,
    ContentIntegrityError,
    MemoryQuery,
    SpaceMismatchError,
    StorageBackend,
    StorageConfig,
    UnknownBackendError,
    open_backend,
)
from cairn.store import Vault

CONTRACT_BACKENDS = ["sqlite", "postgres"]
POSTGRES_URL = os.environ.get("CAIRN_TEST_POSTGRES_URL")

DIMS = 8
EMBED = "contract-embed"
NOW = 1_700_000_000


def unit(i: int) -> np.ndarray:
    """Axis-aligned unit vector: cosine distance 0 to itself, 1 to the others."""
    v = np.zeros(DIMS, dtype=np.float32)
    v[i % DIMS] = 1.0
    return v


def record(key: str, content: str, **over) -> dict:
    rec = {
        "key": key, "canonical_id": over.pop("canonical_id", f"canon-{key}"),
        "content": content, "content_summary": content[:200], "memory_type": "semantic",
        "status": "active", "origin": "agent", "task_id": "task", "agent_id": "agent-a",
        "team_id": "team", "version": 1, "created_at": NOW, "updated_at": NOW,
        "expires_at": None, "archived_at": None, "supersedes": None, "parent_key": None,
        "provenance": None, "confidence": None,
        "content_hash": f"sha256:{content_digest(content)}",
    }
    rec.update(over)
    return rec


FIND_CASES = [
    pytest.param(MemoryQuery(), 100, ["a", "b", "c"], id="all-by-created"),
    pytest.param(MemoryQuery(), 2, ["a", "b"], id="limit"),
    pytest.param(MemoryQuery(eq={"canonical_id": "c1"}), 100, ["a", "b"], id="eq"),
    pytest.param(MemoryQuery(eq={"canonical_id": "c1", "status": "active"}), 100, ["a"],
                 id="eq-and"),
    pytest.param(MemoryQuery(eq={"team_id": "t2"}), 100, ["c"], id="eq-team"),
    pytest.param(MemoryQuery(eq={"canonical_id": "c1"}, exclude_key="a"), 100, ["b"],
                 id="exclude-key"),
    pytest.param(MemoryQuery(updated_before=NOW), 100, ["a", "b"], id="updated-before"),
    pytest.param(MemoryQuery(updated_since=NOW), 100, ["b", "c"], id="updated-since"),
    pytest.param(MemoryQuery(archived_before=NOW), 100, ["c"], id="archived-before"),
    pytest.param(MemoryQuery(archived_before=NOW - 51), 100, [], id="archived-before-none"),
    pytest.param(MemoryQuery(expired_by=NOW), 100, ["c"], id="expired-by"),
    pytest.param(MemoryQuery(expired_by=NOW - 2), 100, [], id="expired-by-none"),
]

FTS_CASES = [
    pytest.param("coolant", {}, ["a", "b"], id="active-only"),
    pytest.param("coolant", {"extra": {"team_id": "t2"}}, ["b"], id="extra-filter"),
    pytest.param("coolant", {"status": "archived"}, ["d"], id="status"),
    pytest.param("coolant pressure", {}, ["a"], id="all-terms"),
]


def interface_members():
    """(name, member) for every public method and property of StorageBackend."""
    for name, member in vars(StorageBackend).items():
        if name.startswith("_"):
            continue
        if callable(member) or isinstance(member, property):
            yield name, member


class TestStorageContract:
    @pytest.fixture(params=CONTRACT_BACKENDS)
    def backend_name(self, request):
        return request.param

    @pytest.fixture
    def open_vault(self, backend_name, tmp_path):
        opened: list[StorageBackend] = []
        schema = None
        config = StorageConfig()
        if backend_name == "postgres":
            if not POSTGRES_URL:
                pytest.skip("CAIRN_TEST_POSTGRES_URL is not set")
            import psycopg
            from psycopg.conninfo import make_conninfo
            from psycopg.sql import SQL, Identifier

            schema = f"cairn_test_{uuid.uuid4().hex}"
            with psycopg.connect(POSTGRES_URL, autocommit=True) as conn:
                conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
            url = make_conninfo(POSTGRES_URL, options=f"-c search_path={schema},public")
            config = StorageConfig(backend=backend_name, url=url)
        else:
            config = StorageConfig(backend=backend_name)

        def _open(create: bool = False, embed: str = EMBED, dims: int = DIMS,
                  doc_threshold: int | None = 64) -> StorageBackend:
            b = open_backend(tmp_path / "vault", embed, dims, config=config,
                             create=create, doc_threshold=doc_threshold)
            opened.append(b)
            return b

        yield _open
        for b in opened:
            b.close()  # close() must be safe to call twice
        if schema is not None:
            from psycopg.sql import SQL, Identifier

            with psycopg.connect(POSTGRES_URL, autocommit=True) as conn:
                conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))

    @pytest.fixture
    def vault(self, open_vault) -> StorageBackend:
        return open_vault(create=True)

    # -- lifecycle -------------------------------------------------------------
    def test_is_a_storage_backend(self, vault, backend_name):
        assert isinstance(vault, StorageBackend)
        assert vault.name == backend_name
        assert vault.vault_dir.is_dir()

    def test_overrides_every_interface_member(self, vault):
        missing = [name for name, member in interface_members()
                   if getattr(type(vault), name) is member]
        assert missing == []

    def test_open_missing_vault_fails(self, open_vault):
        with pytest.raises(FileNotFoundError):
            open_vault(create=False)

    def test_reopen_with_other_embedder_is_refused(self, vault, open_vault):
        vault.close()
        with pytest.raises(SpaceMismatchError):
            open_vault(embed="other-embed")
        with pytest.raises(SpaceMismatchError):
            open_vault(dims=DIMS * 2)

    def test_data_survives_close_and_reopen(self, vault, open_vault):
        vault.insert(record("k1", "persisted fact"), unit(0))
        vault.close()
        again = open_vault()
        assert again.get("k1")["content"] == "persisted fact"

    def test_sync_events_and_peer_cursors_are_persistent(self, vault, open_vault):
        vault.insert(record("sync-k1", "durable sync event"), unit(0))
        pack = vault.export_sync_events()
        assert len(pack["events"]) == 1
        assert pack["events"][0]["kind"] == "snapshot"
        assert pack["events"][0]["snapshot"]["key"] == "sync-k1"
        vault.set_sync_cursor("peer-a", "pull", pack["cursor"], NOW)
        vault.close()
        again = open_vault()
        assert again.get_sync_cursor("peer-a", "pull") == pack["cursor"]
        assert again.list_sync_cursors() == [{
            "peer": "peer-a", "direction": "pull", "cursor": pack["cursor"],
            "updated_at": NOW,
        }]

    def test_initialization_is_idempotent(self, vault, open_vault):
        vault.insert(record("k1", "persisted fact"), unit(0))
        again = open_vault(create=True)
        try:
            assert again.count() == 1
            assert again.get("k1")["content"] == "persisted fact"
        finally:
            again.close()

    def test_close_is_idempotent(self, open_vault):
        v = open_vault(create=True)
        v.close()
        v.close()

    def test_reopen_handle_sees_same_data(self, vault):
        vault.insert(record("k1", "shared fact"), unit(0))
        other = vault.reopen()
        try:
            assert other.get("k1")["key"] == "k1"
            other.insert(record("k2", "written by the second handle"), unit(1))
        finally:
            other.close()
        assert vault.count() == 2

    # -- memories --------------------------------------------------------------------
    def test_insert_and_get_round_trip(self, vault):
        rec = record("k1", "the manifold reads 40 psi", confidence=0.5, provenance="gauge")
        vault.insert(rec, unit(0))
        row = vault.get("k1")
        for f in MEMORY_FIELDS:
            if f in {"state_revision", "state_origin", "state_event_id"}:
                continue
            assert row[f] == rec[f], f
        assert row["state_revision"] == 1
        assert row["state_origin"]
        assert row["state_event_id"]
        assert dict(row)["key"] == "k1"

    def test_get_missing_is_none(self, vault):
        assert vault.get("nope") is None

    def test_by_hash_is_task_and_status_scoped(self, vault):
        rec = record("k1", "same text")
        vault.insert(rec, unit(0))
        vault.insert(record("k2", "same text", task_id="other"), unit(1))
        assert [r["key"] for r in vault.by_hash(rec["content_hash"], "task")] == ["k1"]
        assert vault.by_hash(rec["content_hash"], "task", status="archived") == []

    def test_server_token_lifecycle(self, vault):
        with vault.transaction():
            vault.create_server_token("ct_a", "digest-a", "agent-a", NOW)
            vault.create_server_token("ct_b", "digest-b", "agent-b", NOW + 1, curator=True)

        token = vault.get_server_token("digest-a")
        assert dict(token) == {"token_id": "ct_a", "agent_id": "agent-a",
                               "curator": False, "created_at": NOW}
        listed = [dict(row) for row in vault.list_server_tokens()]
        assert listed == [
            {"token_id": "ct_a", "agent_id": "agent-a", "curator": False,
             "created_at": NOW},
            {"token_id": "ct_b", "agent_id": "agent-b", "curator": True,
             "created_at": NOW + 1},
        ]
        assert vault.get_server_token("unknown") is None
        assert vault.get_agent_ids(["missing"]) == {}

        vault.insert(record("memory-a", "agent one", agent_id="agent-a"), unit(0))
        assert vault.get_agent_ids(["memory-a", "missing"]) == {"memory-a": "agent-a"}
        assert vault.get_agent_ids(["missing"] * 501 + ["memory-a"]) == {
            "memory-a": "agent-a",
        }

        with vault.transaction():
            assert vault.delete_server_token("ct_a") == 1
            assert vault.delete_server_token("missing") == 0
        assert vault.get_server_token("digest-a") is None
        assert [row["token_id"] for row in vault.list_server_tokens()] == ["ct_b"]

        reopened = vault.reopen()
        try:
            assert reopened.get_server_token("digest-b")["agent_id"] == "agent-b"
        finally:
            reopened.close()

    @pytest.mark.parametrize(("query", "limit", "want"), FIND_CASES)
    def test_find_filters(self, vault, query, limit, want):
        vault.insert(record("a", "alpha", created_at=NOW, updated_at=NOW - 100,
                            canonical_id="c1"), unit(0))
        vault.insert(record("b", "beta", created_at=NOW + 1, updated_at=NOW,
                            canonical_id="c1", status="superseded"), unit(1))
        vault.insert(record("c", "gamma", created_at=NOW + 2, updated_at=NOW + 100,
                            team_id="t2", status="archived", archived_at=NOW - 50,
                            expires_at=NOW - 1), unit(2))
        assert [r["key"] for r in vault.find(query, limit)] == want

    def test_find_rejects_unknown_columns(self):
        with pytest.raises(ValueError):
            MemoryQuery(eq={"content; DROP TABLE memories": 1})

    def test_find_embedding_only_on_request(self, vault):
        vault.insert(record("a", "alpha"), unit(3))
        (plain,) = vault.find(MemoryQuery())
        assert "embedding" not in dict(plain)
        (full,) = vault.find(MemoryQuery(), with_embedding=True)
        got = np.frombuffer(bytes(full["embedding"]), dtype=np.float32)
        assert np.array_equal(got, unit(3))

    def test_set_status_archives(self, vault):
        vault.insert(record("a", "alpha"), unit(0))
        assert vault.set_status("a", "archived", NOW + 5, archived_at=NOW + 5) == 1
        row = vault.get("a")
        assert (row["status"], row["updated_at"], row["archived_at"]) == ("archived", NOW + 5, NOW + 5)

    def test_set_status_clears_archived_at(self, vault):
        vault.insert(record("a", "alpha", status="archived", archived_at=NOW), unit(0))
        assert vault.set_status("a", "active", NOW + 6) == 1
        assert vault.get("a")["archived_at"] is None

    def test_set_status_on_missing_key(self, vault):
        assert vault.set_status("missing", "active", NOW) == 0

    def test_counts(self, vault):
        assert vault.count() == 0 and vault.count_by_status() == {}
        vault.insert(record("a", "alpha"), unit(0))
        vault.insert(record("b", "beta", status="archived"), unit(1))
        assert vault.count() == 2
        assert vault.count_by_status() == {"active": 1, "archived": 1}

    def test_delete_by_keys(self, vault):
        vault.insert(record("a", "alpha"), unit(0))
        vault.insert(record("c", "gamma"), unit(2))
        assert vault.delete_by_keys([]) == 0
        assert vault.delete_by_keys(["c", "missing"]) == 1
        assert [r["key"] for r in vault.find(MemoryQuery())] == ["a"]

    def test_delete_by_canonical_clears_every_index(self, vault):
        vault.insert(record("a", "alpha", canonical_id="c1"), unit(0))
        vault.insert(record("b", "beta", canonical_id="c1"), unit(1))
        assert vault.delete_by_canonical("c1") == 2
        assert vault.count() == 0
        assert vault.knn(unit(0), 5) == []
        assert vault.fts_search("alpha") == []

    # -- transactions ------------------------------------------------------------------
    def test_transaction_commits(self, vault, open_vault):
        with vault.transaction():
            vault.insert(record("a", "alpha"), unit(0))
            vault.insert(record("b", "beta"), unit(1))
        vault.close()
        assert open_vault().count() == 2

    def test_transaction_rolls_back_as_one(self, vault):
        vault.insert(record("keep", "kept"), unit(0))
        with pytest.raises(RuntimeError), vault.transaction():
            vault.insert(record("a", "alpha"), unit(1))
            vault.set_status("keep", "archived", NOW + 1)
            with vault.transaction():  # nested joins the outer block
                vault.insert(record("b", "beta"), unit(2))
            raise RuntimeError("abort")
        assert vault.count() == 1
        assert vault.get("keep")["status"] == "active"
        assert vault.knn(unit(1), 5)[0][0]["key"] == "keep"

    def test_failed_insert_leaves_nothing(self, vault):
        vault.insert(record("a", "alpha"), unit(0))
        # Each driver raises its own integrity error for a duplicate key.
        with pytest.raises(Exception):  # noqa: B017
            vault.insert(record("a", "duplicate key"), unit(1))
        assert vault.count() == 1
        assert vault.get("a")["content"] == "alpha"

    # -- keyword search -------------------------------------------------------------------
    @pytest.mark.parametrize(("text", "kwargs", "want"), FTS_CASES)
    def test_fts_matches_and_filters(self, vault, text, kwargs, want):
        vault.insert(record("a", "coolant pump pressure is nominal"), unit(0))
        vault.insert(record("b", "coolant coolant coolant pump", team_id="t2"), unit(1))
        vault.insert(record("c", "unrelated weather note"), unit(2))
        vault.insert(record("d", "coolant leak archived", status="archived"), unit(3))
        assert sorted(r["key"] for r in vault.fts_search(text, **kwargs)) == want

    def test_fts_honours_limit(self, vault):
        vault.insert(record("a", "coolant pump"), unit(0))
        vault.insert(record("b", "coolant valve"), unit(1))
        assert len(vault.fts_search("coolant", limit=1)) == 1

    def test_fts_treats_input_as_literal(self, vault):
        vault.insert(record("a", "plain text"), unit(0))
        for text in ('"*(', "NOT OR AND", "a:b", "'; DROP TABLE memories; --", "   "):
            assert isinstance(vault.fts_search(text), list)
        assert vault.count() == 1

    def test_fts_finds_document_content(self, vault):
        body = "needle " + "haystack " * 40  # over the 64-byte threshold
        vault.insert(record("big", body), unit(0))
        assert [r["key"] for r in vault.fts_search("needle")] == ["big"]

    # -- vector search -------------------------------------------------------------------
    def test_knn_orders_by_cosine_distance(self, vault):
        for i in range(4):
            vault.insert(record(f"k{i}", f"fact {i}"), unit(i))
        q = unit(2) * 0.9 + unit(3) * 0.1
        q /= np.linalg.norm(q)
        hits = vault.knn(q, 2)
        assert [r["key"] for r, _ in hits] == ["k2", "k3"]
        assert hits[0][1] < hits[1][1]
        assert hits[0][1] == pytest.approx(1 - float(q[2]), abs=1e-5)

    def test_knn_filters_before_top_k(self, vault):
        for i in range(20):
            vault.insert(record(f"noise{i}", f"noise {i}", team_id="noise"), unit(0))
        vault.insert(record("want", "wanted", team_id="mine"), unit(1))
        vault.insert(record("gone", "expired", team_id="mine", expires_at=NOW), unit(0))
        vault.insert(record("old", "archived", team_id="mine", status="archived"), unit(0))
        hits = vault.knn(unit(0), 1, filters={"team_id": "mine"}, now=NOW)
        assert [r["key"] for r, _ in hits] == ["want"]
        assert [r["key"] for r, _ in vault.knn(unit(0), 1, status="archived")] == ["old"]

    def test_vec_status_reports_sync(self, vault):
        vault.insert(record("a", "alpha"), unit(0))
        assert isinstance(vault.vec_status()["vec_in_sync"], bool)

    # -- documents ---------------------------------------------------------------------------
    def test_small_content_stays_inline(self, vault):
        assert vault.doc_threshold == 64
        vault.insert(record("s", "short"), unit(0))
        assert vault.get("s")["content"] == "short"
        assert vault.read_content(vault.get("s")) == "short"
        assert vault.doc_stats()["files"] == 0

    def test_large_content_is_a_document(self, vault):
        big = "x" * 65 + " long body"
        vault.insert(record("b", big), unit(1))
        assert vault.get("b")["content"] is None
        assert vault.read_content(vault.get("b")) == big
        assert vault.doc_stats()["files"] == 1

    def test_documents_are_refcounted(self, vault):
        big = "y" * 200
        vault.insert(record("a", big), unit(0))
        vault.insert(record("b", big, task_id="t2"), unit(1))
        assert vault.doc_stats()["files"] == 1
        vault.delete_by_keys(["a"])
        assert vault.read_content(vault.get("b")) == big
        vault.delete_by_keys(["b"])
        assert vault.doc_stats()["files"] == 0
        assert vault.sweep_orphan_docs() == 0

    def test_missing_document_is_loud(self, vault):
        big = "z" * 200
        vault.insert(record("a", big), unit(0))
        row = vault.get("a")
        vault.delete_by_keys(["a"])  # removes the document with its last row
        with pytest.raises(ContentIntegrityError):
            vault.read_content(row)


# -- registry ------------------------------------------------------------------------------
def test_every_registered_backend_is_under_contract():
    assert set(BACKENDS) == set(CONTRACT_BACKENDS)


def test_sqlite_is_the_default_backend(tmp_path):
    v = open_backend(tmp_path, EMBED, DIMS, create=True)
    try:
        assert isinstance(v, Vault)
        assert v.db_path == tmp_path / "vault.db"
    finally:
        v.close()


def test_backend_keyword_remains_supported(tmp_path):
    v = open_backend(tmp_path, EMBED, DIMS, backend="sqlite", create=True)
    try:
        assert v.name == "sqlite"
    finally:
        v.close()


def test_unknown_backend_is_a_clear_error(tmp_path):
    with pytest.raises(UnknownBackendError, match="unknown storage backend 'nosuch'.*postgres.*sqlite"):
        open_backend(tmp_path, EMBED, DIMS,
                     config=StorageConfig(backend="nosuch"), create=True)
    assert not (tmp_path / "vault.db").exists()
