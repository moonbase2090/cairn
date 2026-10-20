"""Mocked PostgreSQL schema migration tests without a live database."""

from __future__ import annotations

from contextlib import nullcontext

import pytest

import cairn.postgres as postgres_module
from cairn.postgres import DOC_THRESHOLD, SCHEMA_VERSION, PostgresVault


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


def make_backend(monkeypatch, *, exists, versions):
    backend = object.__new__(PostgresVault)
    backend._embed_name = "hash"
    backend._dims = 2
    backend._vault_dir = None
    next_version = iter(versions)
    loaded_identity = object()
    calls = {
        "applied": [],
        "checks": [],
        "configure": [],
        "identity_getters": [],
        "queries": [],
        "set_meta": [],
        "transactions": 0,
    }

    def execute(query, params=()):
        statement = " ".join(query.split())
        calls["queries"].append((statement, params))
        if statement == "SELECT to_regclass('cairn_meta') AS relation_name":
            relation_name = "cairn_meta" if exists else None
            return Cursor({"relation_name": relation_name})
        return Cursor()

    def transaction():
        calls["transactions"] += 1
        return nullcontext()

    def load_identity(get_meta):
        calls["identity_getters"].append(get_meta)
        return loaded_identity

    monkeypatch.setattr(backend, "_execute", execute)
    monkeypatch.setattr(backend, "transaction", transaction)
    monkeypatch.setattr(backend, "_migration_version", lambda: next(next_version))
    monkeypatch.setattr(
        backend, "_configure_extension_path", lambda: calls["configure"].append(True)
    )
    monkeypatch.setattr(backend, "_apply_migration", calls["applied"].append)
    monkeypatch.setattr(backend, "_check_space", lambda: calls["checks"].append(True))
    monkeypatch.setattr(
        backend, "_set_meta", lambda key, value: calls["set_meta"].append((key, value))
    )
    monkeypatch.setattr(postgres_module, "load_vault_identity", load_identity)
    return backend, calls, loaded_identity


def test_migrate_rejects_missing_database_when_not_creating(monkeypatch):
    backend, calls, _ = make_backend(monkeypatch, exists=False, versions=[])

    with pytest.raises(FileNotFoundError, match="run `cairn init` first"):
        backend._migrate(create=False, doc_threshold=None)

    assert calls["transactions"] == 0
    assert calls["applied"] == []


def test_migrate_rejects_newer_schema_before_lock(monkeypatch):
    backend, calls, _ = make_backend(
        monkeypatch, exists=True, versions=[SCHEMA_VERSION + 1]
    )

    with pytest.raises(RuntimeError, match="newer than this Cairn version"):
        backend._migrate(create=False, doc_threshold=None)

    assert calls["transactions"] == 0
    assert calls["configure"] == [True]


def test_migrate_reuses_current_schema_and_loads_identity(monkeypatch):
    backend, calls, loaded_identity = make_backend(
        monkeypatch, exists=True, versions=[SCHEMA_VERSION]
    )

    backend._migrate(create=False, doc_threshold=None)

    assert backend._vault_identity is loaded_identity
    assert calls["identity_getters"] == [backend._get_meta]
    assert calls["checks"] == [True]
    assert calls["transactions"] == 0
    assert calls["applied"] == []


def test_migrate_rejects_uninitialized_schema_without_create_before_lock(
    monkeypatch,
):
    backend, calls, _ = make_backend(monkeypatch, exists=True, versions=[0])

    with pytest.raises(FileNotFoundError, match="run `cairn init` first"):
        backend._migrate(create=False, doc_threshold=None)

    assert calls["transactions"] == 0


@pytest.mark.parametrize(
    ("doc_threshold", "expected_threshold"),
    [(None, DOC_THRESHOLD), (512, 512)],
)
def test_migrate_creates_fresh_schema_and_sets_metadata(
    monkeypatch, doc_threshold, expected_threshold
):
    backend, calls, _ = make_backend(monkeypatch, exists=False, versions=[0])

    backend._migrate(create=True, doc_threshold=doc_threshold)

    assert calls["transactions"] == 1
    assert calls["applied"] == list(range(1, SCHEMA_VERSION + 1))
    assert calls["checks"] == [True]
    assert calls["set_meta"] == [
        ("embed_model", "hash"),
        ("dims", "2"),
        ("doc_threshold", str(expected_threshold)),
        ("schema_version", str(SCHEMA_VERSION)),
    ]
    assert any(
        statement.startswith("SELECT pg_advisory_xact_lock")
        for statement, _ in calls["queries"]
    )
    assert any(
        statement.startswith("CREATE TABLE IF NOT EXISTS cairn_meta")
        for statement, _ in calls["queries"]
    )


def test_migrate_upgrades_existing_schema_without_reinitializing_metadata(
    monkeypatch,
):
    backend, calls, _ = make_backend(monkeypatch, exists=True, versions=[2, 2])

    backend._migrate(create=False, doc_threshold=512)

    assert calls["transactions"] == 1
    assert calls["applied"] == [3, 4]
    assert calls["checks"] == [True]
    assert calls["set_meta"] == [("schema_version", str(SCHEMA_VERSION))]


def test_migrate_rechecks_missing_schema_under_lock(monkeypatch):
    backend, calls, _ = make_backend(monkeypatch, exists=True, versions=[1, 0])

    with pytest.raises(FileNotFoundError, match="run `cairn init` first"):
        backend._migrate(create=False, doc_threshold=None)

    assert calls["transactions"] == 1
    assert calls["applied"] == []
    assert calls["set_meta"] == []


def test_migrate_rechecks_schema_version_under_lock(monkeypatch):
    backend, calls, _ = make_backend(
        monkeypatch, exists=True, versions=[1, SCHEMA_VERSION + 1]
    )

    with pytest.raises(RuntimeError, match="newer than this Cairn version"):
        backend._migrate(create=False, doc_threshold=None)

    assert calls["transactions"] == 1
    assert calls["applied"] == []
    assert calls["set_meta"] == []
