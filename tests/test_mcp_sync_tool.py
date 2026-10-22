"""Behavior tests for the MCP sync tool without making network calls."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from cairn import cli, mcp_server
from cairn.mcp_server import call_tool


def _sync_client(tmp_path, *, cursor=0, delta=None):
    calls = {
        "status": [],
        "cursor": [],
        "export": [],
        "saved_cursor": [],
        "import": [],
    }
    vault = SimpleNamespace(
        vault_dir=tmp_path,
        vault_identity=SimpleNamespace(vault_id="vault-identity"),
        sync_origin_id=lambda: "local-origin",
        get_sync_cursor=lambda peer, direction, token_id: (
            calls["cursor"].append((peer, direction, token_id)) or cursor
        ),
        transaction=nullcontext,
        set_sync_cursor=lambda *args: calls["saved_cursor"].append(args),
    )

    def sync_status(origin_id=None):
        calls["status"].append(origin_id)
        return {"origin_id": origin_id, "conflicts": []}

    def export_delta(after):
        calls["export"].append(after)
        return delta or {"cursor": 10, "events": []}

    def import_sync_pack(pack, **kwargs):
        calls["import"].append((pack, kwargs))
        return {"imported": True}

    client = SimpleNamespace(
        agent_id="agent-a",
        vault=vault,
        sync_status=sync_status,
        export_delta=export_delta,
        import_sync_pack=import_sync_pack,
    )
    return client, calls


def _clear_sync_env(monkeypatch):
    monkeypatch.delenv("CAIRN_URL", raising=False)
    monkeypatch.delenv("CAIRN_TOKEN", raising=False)


def _patch_storage_config(monkeypatch, endpoint):
    calls = []

    def storage_config(vault_dir):
        calls.append(vault_dir)
        return SimpleNamespace(sync_endpoint=endpoint)

    monkeypatch.setattr(cli, "storage_config", storage_config)
    return calls


def test_sync_status_without_endpoint_uses_local_vault(tmp_path, monkeypatch):
    _clear_sync_env(monkeypatch)
    config_calls = _patch_storage_config(monkeypatch, None)
    client, calls = _sync_client(tmp_path)

    result = json.loads(call_tool(client, "cairn_sync", {"action": "status"}))

    assert result == {"origin_id": None, "conflicts": []}
    assert calls["status"] == [None]
    assert config_calls == [tmp_path]


def test_sync_status_uses_environment_endpoint_and_token(tmp_path, monkeypatch):
    _clear_sync_env(monkeypatch)
    monkeypatch.setenv("CAIRN_URL", "https://sync.example.test")
    monkeypatch.setenv("CAIRN_TOKEN", "fixture-value")
    _patch_storage_config(monkeypatch, None)
    handshake_calls = []

    def handshake(url, vault_id, **kwargs):
        handshake_calls.append((url, vault_id, kwargs))
        return {"origin_id": "remote-origin"}

    monkeypatch.setattr(mcp_server, "sync_handshake", handshake)
    client, calls = _sync_client(tmp_path)

    result = json.loads(call_tool(client, "cairn_sync", {"action": "status"}))

    assert result == {"origin_id": "remote-origin", "conflicts": []}
    assert handshake_calls == [
        (
            "https://sync.example.test",
            "vault-identity",
            {"token": "fixture-value", "cafile": None},
        )
    ]
    assert calls["status"] == ["remote-origin"]


def test_sync_health_uses_explicit_url_and_ca_file(tmp_path, monkeypatch):
    _clear_sync_env(monkeypatch)
    _patch_storage_config(monkeypatch, None)
    health_calls = []

    def health(url, *, cafile):
        health_calls.append((url, cafile))
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "health_from", health)
    client, _ = _sync_client(tmp_path)

    result = json.loads(
        call_tool(
            client,
            "cairn_sync",
            {
                "action": "health",
                "url": "https://explicit.example.test",
                "tls_ca": "sync-ca.pem",
            },
        )
    )

    assert result == {"ok": True}
    assert health_calls == [("https://explicit.example.test", "sync-ca.pem")]


def test_sync_health_requires_an_endpoint(tmp_path, monkeypatch):
    _clear_sync_env(monkeypatch)
    _patch_storage_config(monkeypatch, None)
    client, _ = _sync_client(tmp_path)

    with pytest.raises(ValueError, match="health needs url or CAIRN_URL"):
        call_tool(client, "cairn_sync", {"action": "health"})


def test_sync_rejects_unknown_action(tmp_path, monkeypatch):
    _clear_sync_env(monkeypatch)
    _patch_storage_config(monkeypatch, None)
    client, _ = _sync_client(tmp_path)

    with pytest.raises(
        ValueError, match="action must be push, pull, status, or health"
    ):
        call_tool(client, "cairn_sync", {"action": "archive"})


def test_sync_push_requires_an_endpoint(tmp_path, monkeypatch):
    _clear_sync_env(monkeypatch)
    _patch_storage_config(monkeypatch, None)
    client, _ = _sync_client(tmp_path)

    with pytest.raises(ValueError, match="push/pull needs url or CAIRN_URL"):
        call_tool(client, "cairn_sync", {"action": "push"})


def test_sync_push_uses_configured_endpoint_and_saves_cursor(tmp_path, monkeypatch):
    _clear_sync_env(monkeypatch)
    config_calls = _patch_storage_config(monkeypatch, "https://configured.example.test")
    handshake_calls = []
    push_calls = []

    def handshake(url, vault_id, **kwargs):
        handshake_calls.append((url, vault_id, kwargs))
        return {"token_id": "token-id", "origin_id": "remote-origin"}

    def push(url, pack, *, token, cafile):
        push_calls.append((url, pack, token, cafile))
        return {"cursor": 8, "added": 2}

    monkeypatch.setattr(mcp_server, "sync_handshake", handshake)
    monkeypatch.setattr(mcp_server, "push_to", push)
    client, calls = _sync_client(tmp_path, cursor=4)

    result = json.loads(
        call_tool(
            client,
            "cairn_sync",
            {"action": "push", "token": "fixture-value", "tls_ca": "sync-ca.pem"},
        )
    )

    assert result == {"cursor": 8, "added": 2}
    assert config_calls == [tmp_path]
    assert handshake_calls == [
        (
            "https://configured.example.test",
            "vault-identity",
            {"token": "fixture-value", "cafile": "sync-ca.pem"},
        )
    ]
    assert calls["cursor"] == [("remote-origin", "push", "token-id")]
    assert calls["export"] == [4]
    assert push_calls == [
        (
            "https://configured.example.test",
            {"cursor": 10, "events": []},
            "fixture-value",
            "sync-ca.pem",
        )
    ]
    saved = calls["saved_cursor"][0]
    assert saved[:3] == ("remote-origin", "push", 8)
    assert saved[4] == "token-id"


@pytest.mark.parametrize("server_cursor", [3, 11])
def test_sync_push_rejects_cursor_outside_exported_range(
    tmp_path, monkeypatch, server_cursor
):
    _clear_sync_env(monkeypatch)
    _patch_storage_config(monkeypatch, None)
    monkeypatch.setattr(
        mcp_server,
        "sync_handshake",
        lambda *_args, **_kwargs: {"token_id": "token-id", "origin_id": "remote"},
    )
    monkeypatch.setattr(
        mcp_server,
        "push_to",
        lambda *_args, **_kwargs: {"cursor": server_cursor},
    )
    client, calls = _sync_client(tmp_path, cursor=4)

    with pytest.raises(ValueError, match="invalid push cursor"):
        call_tool(
            client, "cairn_sync", {"action": "push", "url": "https://sync.example.test"}
        )

    assert calls["saved_cursor"] == []


def test_sync_pull_uses_environment_credentials_and_imports_delta(
    tmp_path, monkeypatch
):
    _clear_sync_env(monkeypatch)
    monkeypatch.setenv("CAIRN_URL", "https://sync.example.test")
    monkeypatch.setenv("CAIRN_TOKEN", "fixture-value")
    _patch_storage_config(monkeypatch, None)
    handshake_calls = []
    pull_calls = []

    def handshake(url, vault_id, **kwargs):
        handshake_calls.append((url, vault_id, kwargs))
        return {"token_id": "token-id", "origin_id": "remote-origin"}

    def pull(url, *, token, cafile, after, peer):
        pull_calls.append((url, token, cafile, after, peer))
        return {"events": [{"key": "memory-a"}]}

    monkeypatch.setattr(mcp_server, "sync_handshake", handshake)
    monkeypatch.setattr(mcp_server, "pull_from", pull)
    client, calls = _sync_client(tmp_path, cursor=6)

    result = json.loads(
        call_tool(client, "cairn_sync", {"action": "pull", "tls_ca": "sync-ca.pem"})
    )

    assert result == {"imported": True}
    assert handshake_calls == [
        (
            "https://sync.example.test",
            "vault-identity",
            {"token": "fixture-value", "cafile": "sync-ca.pem"},
        )
    ]
    assert calls["cursor"] == [("remote-origin", "pull", "token-id")]
    assert pull_calls == [
        (
            "https://sync.example.test",
            "fixture-value",
            "sync-ca.pem",
            6,
            "local-origin",
        )
    ]
    assert calls["import"] == [
        (
            {"events": [{"key": "memory-a"}]},
            {"peer": "remote-origin", "direction": "pull", "token_id": "token-id"},
        )
    ]
