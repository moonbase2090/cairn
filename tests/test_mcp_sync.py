"""MCP entry points for sync state, curator tokens, and conflicts."""

import json
import socket
from types import SimpleNamespace

import pytest

from cairn import mcp_server
from cairn.client import CairnClient
from cairn.embed import HashEmbedder
from cairn.mcp_server import TOOL_DEFS, call_tool
from cairn.store import Vault


def test_mcp_exposes_sync_token_and_conflict_tools(tmp_path):
    embedder = HashEmbedder()
    client = CairnClient(
        Vault(tmp_path / "vault.db", embedder.name, embedder.dims, create=True),
        "agent-a",
        embedder,
    )

    names = {tool["name"] for tool in TOOL_DEFS}
    assert {"cairn_sync", "cairn_serve", "cairn_token", "cairn_conflicts"} <= names

    created = json.loads(
        call_tool(
            client,
            "cairn_token",
            {
                "action": "create",
                "agent": "agent-b",
                "curator": True,
            },
        )
    )
    assert created["curator"] is True
    assert created["token"].startswith("cairn_")
    listed = json.loads(call_tool(client, "cairn_token", {"action": "list"}))
    assert listed[0]["curator"] is True

    status = json.loads(call_tool(client, "cairn_sync", {"action": "status"}))
    assert status["origin_id"] == client.vault.sync_origin_id()
    assert status["conflicts"] == []


def test_mcp_can_start_check_and_stop_sync_server(tmp_path):
    embedder = HashEmbedder()
    client = CairnClient(
        Vault(tmp_path / "vault.db", embedder.name, embedder.dims, create=True),
        "agent-a",
        embedder,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        started = json.loads(
            call_tool(
                client,
                "cairn_serve",
                {
                    "action": "start",
                    "port": port,
                },
            )
        )
        assert started["running"] is True
        status = json.loads(call_tool(client, "cairn_serve", {"action": "status"}))
        assert status["running"] is True
        health = json.loads(call_tool(client, "cairn_serve", {"action": "health"}))
        assert health["ok"] is True
    finally:
        stopped = json.loads(call_tool(client, "cairn_serve", {"action": "stop"}))
    assert stopped["stopped"] is True


def test_conflicts_tool_lists_current_conflicts():
    conflicts = [{"base_key": "base-a", "winner_key": "correction-a"}]
    client = SimpleNamespace(sync_status=lambda: {"conflicts": conflicts})

    result = call_tool(client, "cairn_conflicts", {"action": "list"})

    assert json.loads(result) == conflicts


def test_conflicts_tool_resolves_locally():
    resolution = {"resolved": True, "winner_key": "correction-a"}
    calls = []

    def resolve(base_key, winner_key):
        calls.append((base_key, winner_key))
        return resolution

    client = SimpleNamespace(resolve_competing_correction=resolve)

    result = call_tool(
        client,
        "cairn_conflicts",
        {
            "action": "resolve",
            "base_key": "base-a",
            "winner_key": "correction-a",
        },
    )

    assert json.loads(result) == resolution
    assert calls == [("base-a", "correction-a")]


def test_conflicts_tool_resolves_at_explicit_url(monkeypatch):
    monkeypatch.delenv("CAIRN_URL", raising=False)
    monkeypatch.delenv("CAIRN_TOKEN", raising=False)
    response = {"resolved": True}
    calls = []

    def resolve(url, base_key, winner_key, **kwargs):
        calls.append((url, base_key, winner_key, kwargs))
        return response

    monkeypatch.setattr(mcp_server, "resolve_conflict_to", resolve)
    client = SimpleNamespace(agent_id="agent-a")

    result = call_tool(
        client,
        "cairn_conflicts",
        {
            "action": "resolve",
            "base_key": "base-a",
            "winner_key": "correction-a",
            "url": "https://sync.example.test",
            "token": "explicit-token",
            "tls_ca": "sync-ca.pem",
        },
    )

    assert json.loads(result) == response
    assert calls == [
        (
            "https://sync.example.test",
            "base-a",
            "correction-a",
            {
                "token": "explicit-token",
                "agent_id": "agent-a",
                "cafile": "sync-ca.pem",
            },
        )
    ]


def test_conflicts_tool_uses_sync_environment_defaults(monkeypatch):
    monkeypatch.setenv("CAIRN_URL", "https://env-sync.example.test")
    monkeypatch.setenv("CAIRN_TOKEN", "environment-token")
    calls = []

    def resolve(url, base_key, winner_key, **kwargs):
        calls.append((url, base_key, winner_key, kwargs))
        return {"resolved": True}

    monkeypatch.setattr(mcp_server, "resolve_conflict_to", resolve)
    client = SimpleNamespace(agent_id="agent-b")

    result = call_tool(
        client,
        "cairn_conflicts",
        {
            "action": "resolve",
            "base_key": "base-b",
            "winner_key": "correction-b",
        },
    )

    assert json.loads(result) == {"resolved": True}
    assert calls == [
        (
            "https://env-sync.example.test",
            "base-b",
            "correction-b",
            {"token": "environment-token", "agent_id": "agent-b", "cafile": None},
        )
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "resolve", "winner_key": "correction-a"},
        {"action": "resolve", "base_key": "base-a"},
    ],
)
def test_conflicts_tool_requires_both_resolution_keys(arguments):
    with pytest.raises(ValueError, match="needs base_key and winner_key"):
        call_tool(SimpleNamespace(), "cairn_conflicts", arguments)


def test_conflicts_tool_rejects_unknown_action():
    with pytest.raises(ValueError, match="action must be list or resolve"):
        call_tool(SimpleNamespace(), "cairn_conflicts", {"action": "archive"})
