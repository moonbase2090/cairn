import pytest

from cairn.backup import (
    _clean_error,
    _config_text,
    _validate_timestamp,
    backup_settings,
    backup_status,
    restore,
)


def config(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


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
