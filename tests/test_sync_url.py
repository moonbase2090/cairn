import pytest

from cairn import cli


class SyncClient:
    def export(self):
        return {"memories": []}

    def import_pack(self, pack):
        return {"added": len(pack["memories"]), "skipped": 0}


def test_push_uses_environment_url_and_token(monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_URL", "https://sync.example.com")
    monkeypatch.setenv("CAIRN_TOKEN", "environment-token")
    monkeypatch.setattr(cli, "_open_client", lambda _args: SyncClient())
    calls = []

    def fake_push(url, pack, token, cafile=None):
        calls.append((url, pack, token, cafile))
        return {"added": 1, "skipped": 0}

    monkeypatch.setattr(cli, "push_to", fake_push)

    assert cli.main(["push"]) == 0
    capsys.readouterr()
    assert calls == [("https://sync.example.com", {"memories": []}, "environment-token", None)]


def test_pull_explicit_url_and_token_override_environment(monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_URL", "https://default.example.com")
    monkeypatch.setenv("CAIRN_TOKEN", "environment-token")
    client = SyncClient()
    monkeypatch.setattr(cli, "_open_client", lambda _args: client)
    calls = []

    def fake_pull(url, token, since, cafile=None):
        calls.append((url, token, since, cafile))
        return {"memories": [{"key": "one"}, {"key": "two"}]}

    monkeypatch.setattr(cli, "pull_from", fake_pull)

    assert cli.main(["pull", "https://other.example.com", "--token", "command-token"]) == 0
    capsys.readouterr()
    assert calls == [("https://other.example.com", "command-token", None, None)]


def test_sync_commands_require_url_or_environment(monkeypatch, capsys):
    monkeypatch.delenv("CAIRN_URL", raising=False)
    monkeypatch.setattr(cli, "_open_client", lambda _args: pytest.fail("opened a vault without a URL"))

    with pytest.raises(SystemExit) as exc:
        cli.main(["push"])

    assert exc.value.code == 2
    _out, err = capsys.readouterr()
    assert "pass one or set CAIRN_URL" in err
