"""Behaviour every storage backend must provide. Uses only the StorageBackend API.

TestStorageContract runs once per name in CONTRACT_BACKENDS. A new backend
adds its name there (with a skip mark when its server is not configured) and
must pass unchanged. Nothing in the contract may touch a database driver.
"""
from __future__ import annotations

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
    UnknownBackendError,
    open_backend,
)
from cairn.store import Vault

CONTRACT_BACKENDS = ["sqlite"]

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


class TestStorageContract:
    @pytest.fixture(params=CONTRACT_BACKENDS)
    def backend_name(self, request):
        return request.param

    @pytest.fixture
    def open_vault(self, backend_name, tmp_path):
        opened: list[StorageBackend] = []

        def _open(create: bool = False, embed: str = EMBED, dims: int = DIMS,
                  doc_threshold: int | None = 64) -> StorageBackend:
            b = open_backend(tmp_path / "vault", embed, dims, backend=backend_name,
                             create=create, doc_threshold=doc_threshold)
            opened.append(b)
            return b

        yield _open
        for b in opened:
            b.close()  # close() must be safe to call twice

    @pytest.fixture
    def vault(self, open_vault) -> StorageBackend:
        return open_vault(create=True)

    # -- lifecycle -------------------------------------------------------------
    def test_is_a_storage_backend(self, vault, backend_name):
        assert isinstance(vault, StorageBackend)
        assert vault.name == backend_name
        assert vault.vault_dir.is_dir()

    def test_overrides_every_interface_member(self, vault):
        missing = [
            name for name, member in vars(StorageBackend).items()
            if not name.startswith("_") and (callable(member) or isinstance(member, property))
            and getattr(type(vault), name) is member
        ]
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
            assert row[f] == rec[f], f
        assert dict(row)["key"] == "k1"

    def test_get_missing_is_none(self, vault):
        assert vault.get("nope") is None

    def test_by_hash_is_task_and_status_scoped(self, vault):
        rec = record("k1", "same text")
        vault.insert(rec, unit(0))
        vault.insert(record("k2", "same text", task_id="other"), unit(1))
        assert [r["key"] for r in vault.by_hash(rec["content_hash"], "task")] == ["k1"]
        assert vault.by_hash(rec["content_hash"], "task", status="archived") == []

    def test_find_filters(self, vault):
        vault.insert(record("a", "alpha", created_at=NOW, updated_at=NOW - 100,
                            canonical_id="c1"), unit(0))
        vault.insert(record("b", "beta", created_at=NOW + 1, updated_at=NOW,
                            canonical_id="c1", status="superseded"), unit(1))
        vault.insert(record("c", "gamma", created_at=NOW + 2, updated_at=NOW + 100,
                            team_id="t2", status="archived", archived_at=NOW - 50,
                            expires_at=NOW - 1), unit(2))

        def keys(q, **kw):
            return [r["key"] for r in vault.find(q, **kw)]

        assert keys(MemoryQuery()) == ["a", "b", "c"]  # created_at ascending
        assert keys(MemoryQuery(), limit=2) == ["a", "b"]
        assert keys(MemoryQuery(eq={"canonical_id": "c1"})) == ["a", "b"]
        assert keys(MemoryQuery(eq={"canonical_id": "c1", "status": "active"})) == ["a"]
        assert keys(MemoryQuery(eq={"team_id": "t2"})) == ["c"]
        assert keys(MemoryQuery(eq={"canonical_id": "c1"}, exclude_key="a")) == ["b"]
        assert keys(MemoryQuery(updated_before=NOW)) == ["a", "b"]
        assert keys(MemoryQuery(updated_since=NOW)) == ["b", "c"]
        assert keys(MemoryQuery(archived_before=NOW)) == ["c"]
        assert keys(MemoryQuery(archived_before=NOW - 51)) == []
        assert keys(MemoryQuery(expired_by=NOW)) == ["c"]
        assert keys(MemoryQuery(expired_by=NOW - 2)) == []

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

    def test_set_status(self, vault):
        vault.insert(record("a", "alpha"), unit(0))
        assert vault.set_status("a", "archived", NOW + 5, archived_at=NOW + 5) == 1
        row = vault.get("a")
        assert (row["status"], row["updated_at"], row["archived_at"]) == ("archived", NOW + 5, NOW + 5)
        assert vault.set_status("a", "active", NOW + 6) == 1
        assert vault.get("a")["archived_at"] is None
        assert vault.set_status("missing", "active", NOW) == 0

    def test_counts(self, vault):
        assert vault.count() == 0 and vault.count_by_status() == {}
        vault.insert(record("a", "alpha"), unit(0))
        vault.insert(record("b", "beta", status="archived"), unit(1))
        assert vault.count() == 2
        assert vault.count_by_status() == {"active": 1, "archived": 1}

    def test_delete_by_keys_and_canonical(self, vault):
        vault.insert(record("a", "alpha", canonical_id="c1"), unit(0))
        vault.insert(record("b", "beta", canonical_id="c1"), unit(1))
        vault.insert(record("c", "gamma"), unit(2))
        assert vault.delete_by_keys([]) == 0
        assert vault.delete_by_keys(["c", "missing"]) == 1
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
    def test_fts_ranks_and_filters(self, vault):
        vault.insert(record("a", "coolant pump pressure is nominal"), unit(0))
        vault.insert(record("b", "coolant coolant coolant pump", team_id="t2"), unit(1))
        vault.insert(record("c", "unrelated weather note"), unit(2))
        vault.insert(record("d", "coolant leak archived", status="archived"), unit(3))
        assert {r["key"] for r in vault.fts_search("coolant")} == {"a", "b"}
        assert [r["key"] for r in vault.fts_search("coolant", extra={"team_id": "t2"})] == ["b"]
        assert [r["key"] for r in vault.fts_search("coolant", status="archived")] == ["d"]
        assert [r["key"] for r in vault.fts_search("coolant pressure")] == ["a"]
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
    def test_large_content_is_a_document(self, vault):
        assert vault.doc_threshold == 64
        small, big = "short", "x" * 65 + " long body"
        vault.insert(record("s", small), unit(0))
        vault.insert(record("b", big), unit(1))
        assert vault.get("s")["content"] == small
        assert vault.get("b")["content"] is None
        assert vault.read_content(vault.get("s")) == small
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


def test_unknown_backend_is_a_clear_error(tmp_path):
    with pytest.raises(UnknownBackendError, match="unknown storage backend 'nosuch'.*sqlite"):
        open_backend(tmp_path, EMBED, DIMS, backend="nosuch", create=True)
    assert not (tmp_path / "vault.db").exists()
