import pytest
from contextlib import nullcontext
from types import SimpleNamespace

from cairn import cli


class SyncClient:
    class Vault:
        def __init__(self):
            self.cursors = {}

        vault_identity = SimpleNamespace(vault_id="vault-id", name="test-vault")

        def get_sync_cursor(self, peer, direction, token_id=""):
            return self.cursors.get((peer, direction, token_id), 0)

        def set_sync_cursor(self, peer, direction, cursor, now, token_id=""):
            self.cursors[(peer, direction, token_id)] = cursor

        def transaction(self):
            return nullcontext()

        def sync_origin_id(self):
            return "local-origin"

    def __init__(self):
        self.vault = self.Vault()

    def export_delta(self, after=0):
        return {"pack": "cairn-sync-2", "after": after, "cursor": after,
                "events": [], "origin_id": "local-origin", "vault_id": "vault-id"}

    def import_sync_pack(self, pack, peer=None, direction="pull", token_id=""):
        self.vault.set_sync_cursor(peer, direction, pack["cursor"], 1, token_id)
        return {"added": 0, "updated": 0, "skipped": 0, "cursor": pack["cursor"]}


def test_push_uses_environment_url_and_token(monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_URL", "https://sync.example.com")
    monkeypatch.setenv("CAIRN_TOKEN", "environment-token")
    client = SyncClient()
    monkeypatch.setattr(cli, "_open_client", lambda _args: client)
    monkeypatch.setattr(cli, "sync_handshake", lambda *args, **kwargs: {"token_id": "ct-peer", "origin_id": "remote-origin"})
    calls = []

    def fake_push(url, pack, token, cafile=None):
        calls.append((url, pack, token, cafile))
        return {"added": 1, "skipped": 0, "cursor": pack["cursor"]}

    monkeypatch.setattr(cli, "push_to", fake_push)

    assert cli.main(["push"]) == 0
    capsys.readouterr()
    assert client.vault.cursors[("remote-origin", "push", "ct-peer")] == 0
    assert calls == [(
        "https://sync.example.com",
        {"pack": "cairn-sync-2", "after": 0, "cursor": 0,
         "events": [], "origin_id": "local-origin", "vault_id": "vault-id"},
        "environment-token", None,
    )]


def test_push_uses_configured_aws_sync_endpoint(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("CAIRN_URL", raising=False)
    monkeypatch.delenv("CAIRN_TOKEN", raising=False)
    monkeypatch.setattr(cli, "vault_dir", lambda _args: tmp_path)
    monkeypatch.setattr(
        cli, "storage_config",
        lambda _vdir: SimpleNamespace(sync_endpoint="https://configured.example.com"),
    )
    monkeypatch.setattr(cli, "_open_client", lambda _args: SyncClient())
    monkeypatch.setattr(cli, "sync_handshake", lambda *args, **kwargs: {"token_id": "ct-peer"})
    calls = []
    monkeypatch.setattr(
        cli, "push_to",
        lambda url, pack, token, cafile=None: calls.append((url, token))
        or {"added": 0, "skipped": 0, "cursor": pack["cursor"]},
    )

    assert cli.main(["push"]) == 0
    capsys.readouterr()

    assert calls == [("https://configured.example.com", "")]


def test_pull_explicit_url_and_token_override_environment(monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_URL", "https://default.example.com")
    monkeypatch.setenv("CAIRN_TOKEN", "environment-token")
    client = SyncClient()
    monkeypatch.setattr(cli, "_open_client", lambda _args: client)
    monkeypatch.setattr(cli, "sync_handshake", lambda *args, **kwargs: {"token_id": "ct-peer", "origin_id": "remote-origin"})
    calls = []

    def fake_pull(url, token, since=None, cafile=None, after=None, peer=None):
        calls.append((url, token, since, cafile, after, peer))
        return {"pack": "cairn-sync-2", "after": 0, "cursor": 0,
                "events": [], "embed_model": "hash-v2", "dims": 384,
                "vault_id": "vault-id"}

    monkeypatch.setattr(cli, "pull_from", fake_pull)

    assert cli.main(["pull", "https://other.example.com", "--token", "command-token"]) == 0
    capsys.readouterr()
    assert client.vault.cursors[("remote-origin", "pull", "ct-peer")] == 0
    assert calls == [(
        "https://other.example.com", "command-token", None, None, 0, "local-origin",
    )]


def test_sync_commands_require_url_or_environment(monkeypatch, capsys):
    monkeypatch.delenv("CAIRN_URL", raising=False)
    monkeypatch.setattr(cli, "_open_client", lambda _args: pytest.fail("opened a vault without a URL"))

    with pytest.raises(SystemExit) as exc:
        cli.main(["push"])

    assert exc.value.code == 2
    _out, err = capsys.readouterr()
    assert "pass one, set CAIRN_URL, or configure AWS sync_endpoint" in err
