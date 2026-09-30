import json
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest

from cairn import backup, cli
from cairn.backup import (
    BackupSettings,
    BackupTarget,
    _clean_error,
    _config_text,
    _document_refs,
    _fetch_document,
    _install_documents,
    _replication_lock,
    _restore_metadata,
    _run_litestream,
    _run_target_once,
    _sync_documents,
    _validate_timestamp,
    backup_settings,
    backup_status,
    replicate,
    restore,
)
from cairn.models import content_digest


def config(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def local_target(vault: Path, target_path: Path) -> BackupTarget:
    config(vault / "config.toml", f'[[backup]]\nkind = "file"\npath = "{target_path}"\n')
    return backup_settings(vault, home=vault.parent / "home").targets[0]


def sqlite_refs(path: Path, refs=()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE memories (content_ref TEXT)")
        connection.executemany("INSERT INTO memories VALUES (?)", ((ref,) for ref in refs))


def put_source_document(vault: Path, content: str) -> str:
    digest = content_digest(content)
    ref = f"sha256:{digest}"
    path = vault / "docs" / digest[:2] / digest[2:4] / f"{digest}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return ref


def test_backup_targets_read_from_vault_before_home(tmp_path):
    vault = tmp_path / "project" / ".cairn"
    home = tmp_path / "home"
    config(home / ".cairn" / "config.toml", '[[backup]]\nkind = "file"\npath = "~/old"\n')
    config(vault / "config.toml", '[[backup]]\nkind = "s3"\nbucket = "cairn"\n')

    settings = backup_settings(vault, home=home)

    assert [target.name for target in settings.targets] == ["s3"]
    assert settings.targets[0].path.startswith("cairn/")


def test_backup_targets_from_home_can_recover_a_missing_vault(tmp_path):
    vault = tmp_path / "project" / ".cairn"
    home = tmp_path / "home"
    config(
        home / ".cairn" / "config.toml",
        '[[backup]]\nkind = "s3"\nbucket = "cairn"\nname = "primary"\n',
    )

    settings = backup_settings(vault, home=home)

    assert settings.targets[0].name == "primary"
    assert not vault.exists()


def test_xdg_config_survives_removal_of_the_default_vault(tmp_path):
    vault = tmp_path / "home" / ".cairn"
    home = vault.parent
    config(
        home / ".config" / "cairn" / "config.toml",
        '[[backup]]\nkind = "s3"\nbucket = "cairn"\nname = "primary"\n',
    )

    settings = backup_settings(vault, home=home)

    assert settings.targets[0].name == "primary"
    assert not vault.exists()


def test_duplicate_default_target_names_get_stable_ordinals(tmp_path):
    vault = tmp_path / "vault"
    config(
        vault / "config.toml",
        '[[backup]]\nkind = "s3"\nbucket = "one"\n'
        '[[backup]]\nkind = "s3"\nbucket = "two"\n',
    )

    settings = backup_settings(vault, home=tmp_path / "home")

    assert [target.name for target in settings.targets] == ["s3", "s3-2"]


def test_backup_config_rejects_inline_credentials(tmp_path):
    vault = tmp_path / "vault"
    config(
        vault / "config.toml",
        '[[backup]]\nkind = "s3"\nbucket = "cairn"\nsecret_access_key = "secret"\n',
    )

    with pytest.raises(ValueError, match="credentials belong in standard environment or profile"):
        backup_settings(vault, home=tmp_path / "home")


def test_local_backup_cannot_nest_inside_the_vault(tmp_path):
    vault = tmp_path / "vault"
    config(vault / "config.toml", '[[backup]]\nkind = "file"\npath = "backups"\n')

    with pytest.raises(ValueError, match="outside the vault directory"):
        backup_settings(vault, home=tmp_path / "home")


def test_backup_target_rejects_endpoint_credentials(tmp_path):
    vault = tmp_path / "vault"
    config(
        vault / "config.toml",
        '[[backup]]\nkind = "s3"\nbucket = "cairn"\n'
        'endpoint = "https://user:secret@example.test"\n',
    )

    with pytest.raises(ValueError, match="without credentials"):
        backup_settings(vault, home=tmp_path / "home")


def test_restore_refuses_to_replace_an_existing_database(tmp_path):
    vault = tmp_path / "vault"
    config(vault / "config.toml", '[[backup]]\nkind = "s3"\nbucket = "cairn"\n')
    database = vault / "vault.db"
    database.write_bytes(b"keep this database")
    settings = backup_settings(vault, home=tmp_path / "home")

    with pytest.raises(FileExistsError, match="move it aside"):
        restore(vault, settings, "s3")

    assert database.read_bytes() == b"keep this database"


def test_external_error_text_redacts_standard_credentials(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "local-test-secret")

    message = _clean_error("request failed with local-test-secret", 1)

    assert "local-test-secret" not in message
    assert "[redacted]" in message


def test_litestream_config_contains_target_settings_but_no_secrets(tmp_path):
    vault = tmp_path / "vault"
    config(vault / "config.toml", '[[backup]]\nkind = "s3"\nbucket = "cairn"\nendpoint = "http://127.0.0.1:9000"\n')
    target = backup_settings(vault, home=tmp_path / "home").targets[0]

    rendered = _config_text(target, vault / "vault.db", vault / "backup" / "s3")

    assert 'bucket: "cairn"' in rendered
    assert 'endpoint: "http://127.0.0.1:9000"' in rendered
    assert "access-key-id" not in rendered
    assert "secret-access-key" not in rendered


def test_restore_timestamp_requires_timezone():
    assert _validate_timestamp("2026-09-29T18:00:00-06:00") == "2026-09-30T00:00:00Z"
    with pytest.raises(ValueError, match="include a timezone"):
        _validate_timestamp("2026-09-29T18:00:00")


def test_backup_status_reports_configured_targets_without_opening_vault(tmp_path):
    vault = tmp_path / "missing-vault"
    config(
        tmp_path / "home" / ".cairn" / "config.toml",
        '[[backup]]\nkind = "file"\npath = "' + str(tmp_path / "replica") + '"\n',
    )
    settings = backup_settings(vault, home=tmp_path / "home")

    status = backup_status(vault, settings)

    assert status == {
        "running": False,
        "targets": [{
            "name": "local", "kind": "file", "last_success": None,
            "last_error": None, "last_snapshot": None,
        }],
    }
    assert not vault.exists()


def test_document_sync_is_idempotent_and_restore_keeps_content_address(tmp_path):
    vault = tmp_path / "vault"
    content = "The large document survives a local backup. " * 60
    ref = put_source_document(vault, content)
    database = vault / "vault.db"
    sqlite_refs(database, [ref])
    target = local_target(vault, tmp_path / "replica")

    _sync_documents(target, database, vault)
    _sync_documents(target, database, vault)

    restored_vault = tmp_path / "restored" / ".cairn"
    restored_vault.mkdir(parents=True)
    _install_documents(database, target, restored_vault)
    assert _fetch_document(target, ref) == content.encode()
    assert list((tmp_path / "replica" / "cairn-docs").rglob("*.md"))
    assert next((restored_vault / "docs").rglob("*.md")).read_text() == content


def test_document_refs_handle_old_schema_and_reject_unknown_hashes(tmp_path):
    old_db = tmp_path / "old.db"
    with sqlite3.connect(old_db) as connection:
        connection.execute("CREATE TABLE memories (content TEXT)")
    assert _document_refs(old_db) == []

    current_db = tmp_path / "current.db"
    sqlite_refs(current_db, ["plain-hash"])
    with pytest.raises(OSError, match="unsupported external document reference"):
        _document_refs(current_db)


def test_run_target_once_resyncs_documents_committed_during_replication(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    database = vault / "vault.db"
    sqlite_refs(database)
    target = local_target(vault, tmp_path / "replica")
    content = "created while Litestream is taking its WAL copy " * 50
    sync_calls = []
    sync = backup._sync_documents

    def track_sync(target_arg, db_arg, vault_arg):
        sync_calls.append(1)
        sync(target_arg, db_arg, vault_arg)

    def concurrent_write(_binary, _args, stop=None):
        ref = put_source_document(vault, content)
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO memories VALUES (?)", (ref,))
        return 0, ""

    monkeypatch.setattr(backup, "_sync_documents", track_sync)
    monkeypatch.setattr(backup, "_run_litestream", concurrent_write)
    state = {"targets": {}}

    _run_target_once(target, database, vault, "litestream", state)

    digest = content_digest(content)
    remote = tmp_path / "replica" / "cairn-docs" / digest[:2] / digest[2:4] / f"{digest}.md"
    assert len(sync_calls) == 2
    assert remote.read_text() == content
    assert state["targets"]["local"]["last_success"]
    assert state["targets"]["local"]["last_error"] is None


def test_replication_lock_reports_contention_and_releases(tmp_path):
    vault = tmp_path / "vault"
    with _replication_lock(vault, blocking=False) as held:
        assert held is not None
        with _replication_lock(vault, blocking=False) as contender:
            assert contender is None
    with _replication_lock(vault, blocking=False) as acquired_again:
        assert acquired_again is not None


def test_run_litestream_returns_process_result(monkeypatch):
    class Process:
        returncode = 7

        def communicate(self, timeout=None):
            return "", "failed"

    monkeypatch.setattr(backup.subprocess, "Popen", lambda *args, **kwargs: Process())
    assert _run_litestream("litestream", ["replicate"]) == (7, "failed")


@pytest.mark.parametrize("kill", [False, True])
def test_run_litestream_stops_or_kills_after_shutdown(monkeypatch, kill):
    class Process:
        returncode = -15

        def __init__(self):
            self.calls = 0
            self.terminated = False
            self.killed = False

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1 or (kill and self.calls == 2):
                raise subprocess.TimeoutExpired("litestream", timeout)
            return "", "stopped"

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    process = Process()
    monkeypatch.setattr(backup.subprocess, "Popen", lambda *args, **kwargs: process)
    stop = threading.Event()
    stop.set()

    result = _run_litestream("litestream", ["replicate"], stop)

    assert result == (-15, "stopped")
    assert process.terminated
    assert process.killed is kill


def test_replicate_runs_until_requested_shutdown(tmp_path, monkeypatch, capsys):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "vault.db").write_bytes(b"database")
    target = local_target(vault, tmp_path / "replica")
    calls = []

    def run_once(_target, _database, _vault, _binary, _state, stop):
        calls.append(1)
        stop.set()

    monkeypatch.setattr(backup, "_litestream", lambda: "litestream")
    monkeypatch.setattr(backup, "_run_target_once", run_once)

    assert replicate(vault, BackupSettings((target,))) == 0
    assert len(calls) == 1
    assert "replicating SQLite backups" in capsys.readouterr().out


def test_restore_rebuilds_metadata_from_database(tmp_path, monkeypatch):
    vault = tmp_path / "project" / ".cairn"
    vault.mkdir(parents=True)
    database = vault / "vault.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE meta (k TEXT, v TEXT)")
        connection.executemany("INSERT INTO meta VALUES (?, ?)", [("embed_model", "hash"), ("dims", "8")])
        connection.execute("CREATE TABLE memories (team_id TEXT, agent_id TEXT)")
        connection.execute("INSERT INTO memories VALUES ('team-a', 'agent-a')")

    _restore_metadata(vault, database)

    project = json.loads((vault / "project.json").read_text())
    assert project["team"] == "team-a"
    assert project["agent_id"] == "agent-a"
    assert (vault / "embedder").read_text()
    assert {"vault.db-wal", "vault.db-shm"} <= set((vault / ".gitignore").read_text().splitlines())


def test_restore_uses_litestream_output_without_overwriting(tmp_path, monkeypatch):
    vault = tmp_path / "project" / ".cairn"
    target = local_target(vault, tmp_path / "replica")
    settings = BackupSettings((target,))
    monkeypatch.setattr(backup, "_litestream", lambda: "litestream")

    def write_restored_database(_binary, args, stop=None):
        output = args[args.index("-o") + 1]
        with sqlite3.connect(output) as connection:
            connection.execute("CREATE TABLE meta (k TEXT, v TEXT)")
            connection.execute("CREATE TABLE memories (team_id TEXT, agent_id TEXT, content_ref TEXT)")
            connection.execute("INSERT INTO memories VALUES ('team-b', 'agent-b', NULL)")
        return 0, ""

    monkeypatch.setattr(backup, "_run_litestream", write_restored_database)

    result = restore(vault, settings, "local")

    assert result["target"] == "local"
    assert (vault / "vault.db").is_file()
    assert json.loads((vault / "project.json").read_text())["team"] == "team-b"


def test_backup_cli_status_supports_text_and_json(tmp_path, monkeypatch, capsys):
    vault = tmp_path / "vault"
    config(vault / "config.toml", f'[[backup]]\nkind = "file"\npath = "{tmp_path / "replica"}"\n')
    monkeypatch.setattr(cli, "backup_status", lambda *_: {
        "running": True,
        "targets": [{"name": "local", "kind": "file", "last_success": "now", "last_error": None}],
    })
    args = type("Args", (), {"vault": str(vault), "backup_action": "status", "json": False})()

    assert cli._run_backup_command(args) == 0
    assert "local (file): now" in capsys.readouterr().out
    args.json = True
    assert cli._run_backup_command(args) == 0
    assert '"running": true' in capsys.readouterr().out
