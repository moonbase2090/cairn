"""Persistent logical vault identity and its one-time legacy migration."""
from __future__ import annotations

import sqlite3

import pytest

from cairn.store import Vault


def test_identity_is_stable_and_uses_project_label(tmp_path):
    vdir = tmp_path / "private-vault"
    vdir.mkdir()
    (vdir / "project.json").write_text('{"project":"Acme Research"}')
    db_path = vdir / "vault.db"

    vault = Vault(db_path, "hash", 8, create=True)
    first = vault.vault_identity
    vault.close()

    reopened = Vault(db_path, "hash", 8)
    assert reopened.vault_identity == first
    assert first.name == "Acme Research"
    assert str(vdir) not in first.name
    reopened.close()


def test_v3_vault_migration_preserves_cursor_and_splits_token_scope(tmp_path):
    vdir = tmp_path / "legacy-vault"
    vdir.mkdir()
    (vdir / "project.json").write_text('{"project":"Legacy Team"}')
    db_path = vdir / "vault.db"
    vault = Vault(db_path, "hash", 8, create=True)
    vault.set_sync_cursor("old-peer", "pull", 17, 23)
    vault.set_sync_cursor("ct_abc123:old-origin", "push", 19, 29)
    vault.close()

    connection = sqlite3.connect(db_path)
    connection.execute(
        "DELETE FROM meta WHERE k IN ('vault_id', 'vault_name', 'vault_identity_migration')"
    )
    connection.execute("UPDATE meta SET v='3' WHERE k='schema'")
    connection.execute("DROP TABLE sync_cursors")
    connection.execute("""
        CREATE TABLE sync_cursors(
          peer TEXT NOT NULL, direction TEXT NOT NULL, cursor INTEGER NOT NULL,
          updated_at INTEGER NOT NULL, PRIMARY KEY(peer, direction)
        )
    """)
    connection.execute(
        "INSERT INTO sync_cursors VALUES('old-peer', 'pull', 17, 23)"
    )
    connection.execute(
        "INSERT INTO sync_cursors VALUES('ct_abc123:old-origin', 'push', 19, 29)"
    )
    connection.commit()
    connection.close()

    migrated = Vault(db_path, "hash", 8)
    assert migrated.vault_identity.name == "Legacy Team"
    assert migrated.get_sync_cursor("old-peer", "pull") == 17
    assert migrated.get_sync_cursor("old-origin", "push", "ct_abc123") == 19
    migrated.close()


def test_migrated_vault_with_missing_identity_fails_closed(tmp_path):
    db_path = tmp_path / "vault" / "vault.db"
    vault = Vault(db_path, "hash", 8, create=True)
    vault.close()

    connection = sqlite3.connect(db_path)
    connection.execute("DELETE FROM meta WHERE k='vault_id'")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="identity is incomplete"):
        Vault(db_path, "hash", 8)
