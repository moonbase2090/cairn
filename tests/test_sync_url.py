import pytest
from contextlib import nullcontext

from cairn import cli


class SyncClient:
    class Vault:
        def __init__(self):
            self.cursors = {}

        def get_sync_cursor(self, peer, direction):
            return self.cursors.get((peer, direction), 0)

        def set_sync_cursor(self, peer, direction, cursor, now):
            self.cursors[(peer, direction)] = cursor

        def transaction(self):
            return nullcontext()

        def sync_origin_id(self):
            return "local-origin"

    def __init__(self):
        self.vault = self.Vault()

    def export_delta(self, after=0):
        return {"pack": "cairn-sync-2", "after": after, "cursor": after,
                "events": [], "origin_id": "local-origin"}

    def import_sync_pack(self, pack, peer=None, direction="pull"):
        self.vault.set_sync_cursor(peer, direction, pack["cursor"], 1)
        return {"added": 0, "updated": 0, "skipped": 0, "cursor": pack["cursor"]}


def test_push_uses_environment_url_and_token(monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_URL", "https://sync.example.com")
    monkeypatch.setenv("CAIRN_TOKEN", "environment-token")
    monkeypatch.setattr(cli, "_open_client", lambda _args: SyncClient())
    calls = []

    def fake_push(url, pack, token, cafile=None):
        calls.append((url, pack, token, cafile))
        return {"added": 1, "skipped": 0, "cursor": pack["cursor"]}

    monkeypatch.setattr(cli, "push_to", fake_push)

    assert cli.main(["push"]) == 0
    capsys.readouterr()
    assert calls == [(
        "https://sync.example.com",
        {"pack": "cairn-sync-2", "after": 0, "cursor": 0,
         "events": [], "origin_id": "local-origin"},
        "environment-token", None,
    )]


def test_pull_explicit_url_and_token_override_environment(monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_URL", "https://default.example.com")
    monkeypatch.setenv("CAIRN_TOKEN", "environment-token")
    client = SyncClient()
    monkeypatch.setattr(cli, "_open_client", lambda _args: client)
    calls = []

    def fake_pull(url, token, since=None, cafile=None, after=None, peer=None):
        calls.append((url, token, since, cafile, after, peer))
        return {"pack": "cairn-sync-2", "after": 0, "cursor": 0,
                "events": [], "embed_model": "hash-v2", "dims": 384}

    monkeypatch.setattr(cli, "pull_from", fake_pull)

    assert cli.main(["pull", "https://other.example.com", "--token", "command-token"]) == 0
    capsys.readouterr()
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
    assert "pass one or set CAIRN_URL" in err
