"""Public CLI routing behavior for backup and sync commands."""

from types import SimpleNamespace

import pytest

from cairn import cli


def _patch_default_context(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "vault_dir", lambda _args: tmp_path)
    monkeypatch.setattr(cli, "default_agent_id", lambda _vault_dir: "agent-a")


def test_sync_preflight_reports_storage_configuration_error(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.delenv("CAIRN_URL", raising=False)
    _patch_default_context(monkeypatch, tmp_path)

    def invalid_storage_config(_vault_dir):
        raise ValueError("invalid sync configuration")

    monkeypatch.setattr(cli, "storage_config", invalid_storage_config)

    with pytest.raises(SystemExit) as exc:
        cli.main(["push"])

    assert exc.value.code == 2
    _out, error = capsys.readouterr()
    assert "invalid sync configuration" in error


def test_sync_status_uses_configured_endpoint(monkeypatch, tmp_path):
    monkeypatch.delenv("CAIRN_URL", raising=False)
    _patch_default_context(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli,
        "storage_config",
        lambda _vault_dir: SimpleNamespace(sync_endpoint="https://sync.example.test"),
    )
    routed = []
    monkeypatch.setattr(cli, "_open_client", lambda _args: object())

    def run_command(args, _client, flag_agent_id):
        routed.append((args.cmd, args.sync_action, args.url, flag_agent_id))
        return 0

    monkeypatch.setattr(cli, "_run_command", run_command)

    result = cli.main(["sync", "status"])

    assert result == 0
    assert routed == [("sync", "status", "https://sync.example.test", None)]


def test_embedd_command_dispatches_without_opening_a_vault(monkeypatch, tmp_path):
    _patch_default_context(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(cli, "_cmd_embedd", lambda args: calls.append(args) or 7)

    result = cli.main(["embedd"])

    assert result == 7
    assert len(calls) == 1
    assert calls[0].spec == "fastembed"


def test_backup_command_dispatches_without_opening_a_vault(monkeypatch, tmp_path):
    _patch_default_context(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        cli, "_run_backup_command", lambda args: calls.append(args) or 8
    )

    result = cli.main(["backup", "status"])

    assert result == 8
    assert len(calls) == 1
    assert calls[0].backup_action == "status"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ["restore", "memory-a", "--from", "snapshot-a"],
            "restore --from cannot be combined with a memory key",
        ),
        (["restore", "--at", "2026-10-19T12:00:00Z"], "restore --at requires --from"),
        (["restore"], "restore needs a memory key or --from"),
    ],
)
def test_restore_rejects_incomplete_or_conflicting_arguments(
    monkeypatch, tmp_path, capsys, arguments, message
):
    _patch_default_context(monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as exc:
        cli.main(arguments)

    assert exc.value.code == 2
    _out, error = capsys.readouterr()
    assert message in error


def test_restore_from_dispatches_backup_restore(monkeypatch, tmp_path):
    _patch_default_context(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        cli,
        "_restore_backup_command",
        lambda args: calls.append(args) or 9,
    )

    result = cli.main(["restore", "--from", "snapshot-a"])

    assert result == 9
    assert len(calls) == 1
    assert calls[0].from_target == "snapshot-a"
