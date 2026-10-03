"""MCP entry points for sync state, curator tokens, and conflicts."""
import json
import socket

from cairn.client import CairnClient
from cairn.embed import HashEmbedder
from cairn.mcp_server import TOOL_DEFS, call_tool
from cairn.store import Vault


def test_mcp_exposes_sync_token_and_conflict_tools(tmp_path):
    embedder = HashEmbedder()
    client = CairnClient(
        Vault(tmp_path / "vault.db", embedder.name, embedder.dims, create=True),
        "agent-a", embedder,
    )

    names = {tool["name"] for tool in TOOL_DEFS}
    assert {"cairn_sync", "cairn_serve", "cairn_token", "cairn_conflicts"} <= names

    created = json.loads(call_tool(client, "cairn_token", {
        "action": "create", "agent": "agent-b", "curator": True,
    }))
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
        "agent-a", embedder,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        started = json.loads(call_tool(client, "cairn_serve", {
            "action": "start", "port": port,
        }))
        assert started["running"] is True
        status = json.loads(call_tool(client, "cairn_serve", {"action": "status"}))
        assert status["running"] is True
        health = json.loads(call_tool(client, "cairn_serve", {"action": "health"}))
        assert health["ok"] is True
    finally:
        stopped = json.loads(call_tool(client, "cairn_serve", {"action": "stop"}))
    assert stopped["stopped"] is True
