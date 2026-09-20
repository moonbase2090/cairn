"""Onboarding tests — interactive init, bootstrap, MCP stdio session."""
import json
import os
import subprocess
import sys

import cairn.cli as cli_mod
from cairn.cli import main


def run(argv, capsys):
    rc = main(argv)
    return rc, *capsys.readouterr()


def test_init_yes_writes_project_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "claude-myproj")
    rc, out, _ = run(["init", "--yes", "--json"], capsys)
    assert rc == 0
    proj = json.loads((tmp_path / ".cairn" / "project.json").read_text())
    assert proj["agent_id"] == "claude-myproj"
    assert proj["team"] and proj["project"]


def test_init_interactive_prompts(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.delenv("CAIRN_AGENT", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["My Proj", "myproj", "myteam", "claude-myproj"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    rc, _, _ = run(["init"], capsys)
    assert rc == 0
    proj = json.loads((tmp_path / ".cairn" / "project.json").read_text())
    assert proj == {"project": "My Proj", "slug": "myproj", "team": "myteam",
                    "agent_id": "claude-myproj"}


def test_bootstrap_wires_project(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "claude-myproj")
    monkeypatch.chdir(tmp_path)
    assert run(["init", "--yes"], capsys)[0] == 0
    rc, out, _ = run(["bootstrap", "--json"], capsys)
    assert rc == 0
    body = json.loads(out)
    assert body["seeded"] == {"created": 5, "unchanged": 0}

    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert mcp["mcpServers"]["cairn"]["command"] == "cairn-mcp"
    assert mcp["mcpServers"]["cairn"]["env"]["CAIRN_AGENT"] == "claude-myproj"
    gi = (tmp_path / ".cairn" / ".gitignore").read_text()
    assert "vault.db-wal" in gi and "vault.db-shm" in gi

    agents = (tmp_path / "AGENTS.md").read_text()
    assert agents.count("<!-- cairn:begin -->") == 1

    # onboarding pack is retrievable like any other memory
    rc, out, _ = run(["retrieve", "how do I use shared memory?", "--task", "cairn-onboarding", "--json"], capsys)
    assert rc == 0 and len(json.loads(out)) == 5

    # re-bootstrap is idempotent: one section, pack unchanged
    rc, out, _ = run(["bootstrap", "--json"], capsys)
    assert rc == 0 and json.loads(out)["seeded"] == {"created": 0, "unchanged": 5}
    assert (tmp_path / "AGENTS.md").read_text().count("<!-- cairn:begin -->") == 1


def test_mcp_stdio_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "mcp-probe")
    monkeypatch.chdir(tmp_path)
    env = dict(os.environ, CAIRN_DIR=str(tmp_path / ".cairn"), CAIRN_AGENT="mcp-probe")

    def rpc(payloads):
        proc = subprocess.run(
            [sys.executable, "-m", "cairn.mcp_server"],
            input="\n".join(json.dumps(p) for p in payloads) + "\n",
            capture_output=True, text=True, timeout=120, cwd="/home/brandan/Projects/VectorVault-cli",
            env={**env, "PYTHONPATH": "/home/brandan/Projects/VectorVault-cli/src"},
        )
        assert proc.returncode == 0, proc.stderr
        return {json.loads(line)["id"]: json.loads(line) for line in proc.stdout.splitlines() if line.strip()}

    # vault must exist first (server refuses without init)
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["init", "--yes", "--embed-spec", "hash"]) == 0

    res = rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "cairn_howto", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "store_memory",
                    "arguments": {"content": "mcp probe memory alpha beta", "team_id": "t", "task_id": "k"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "retrieve_memory",
                    "arguments": {"query": "mcp probe memory", "task_id": "k"}}},
    ])
    assert res[1]["result"]["serverInfo"]["name"] == "cairn"
    assert {t["name"] for t in res[2]["result"]["tools"]} >= {
        "retrieve_memory", "store_memory", "list_memories", "get_memory",
        "archive_memory", "restore_memory", "cairn_howto", "cairn_whoami",
        "cairn_gc", "cairn_export", "cairn_import", "cairn_ingest"}
    assert "shared memory" in res[3]["result"]["content"][0]["text"].lower()
    assert json.loads(res[4]["result"]["content"][0]["text"])["action"] == "created"
    hits = json.loads(res[5]["result"]["content"][0]["text"])
    assert hits and "mcp probe memory" in hits[0]["content"]


def test_mcp_ops_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "mcp-ops")
    env = dict(os.environ, CAIRN_DIR=str(tmp_path / ".cairn"), CAIRN_AGENT="mcp-ops")
    import io
    from contextlib import redirect_stdout
    with redirect_stdout(io.StringIO()):
        assert main(["init", "--yes", "--embed-spec", "hash"]) == 0

    def rpc(payloads):
        proc = subprocess.run(
            [sys.executable, "-m", "cairn.mcp_server"],
            input="\n".join(json.dumps(p) for p in payloads) + "\n",
            capture_output=True, text=True, timeout=120, cwd="/home/brandan/Projects/VectorVault-cli",
            env={**env, "PYTHONPATH": "/home/brandan/Projects/VectorVault-cli/src"},
        )
        assert proc.returncode == 0, proc.stderr
        return {json.loads(line)["id"]: json.loads(line) for line in proc.stdout.splitlines() if line.strip()}

    res = rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "store_memory",
                    "arguments": {"content": "ops tool memory one", "team_id": "t", "task_id": "k"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "cairn_whoami", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "cairn_export", "arguments": {}}},
    ])
    me = json.loads(res[2]["result"]["content"][0]["text"])
    assert me["agent"] == "mcp-ops" and me["memories"] == 1
    pack = json.loads(res[3]["result"]["content"][0]["text"])
    assert len(pack["memories"]) == 1

    res = rpc([
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "cairn_import", "arguments": {"pack": pack}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "cairn_gc", "arguments": {}}},
    ])
    assert json.loads(res[4]["result"]["content"][0]["text"])["skipped"] == 1
    assert json.loads(res[5]["result"]["content"][0]["text"])["dry_run"] is True


def test_init_yes_refuses_without_identity(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.delenv("CAIRN_AGENT", raising=False)
    monkeypatch.setattr(cli_mod, "home_config_agent_id", lambda: None)
    rc, _, err = run(["init", "--yes"], capsys)
    assert rc == 2 and "--agent-id" in err


def test_bootstrap_agent_override_and_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "claude-seat")
    monkeypatch.chdir(tmp_path)
    assert run(["init", "--yes"], capsys)[0] == 0
    # override the seat at bootstrap time
    rc, out, _ = run(["bootstrap", "--agent-id", "grok-seat", "--json"], capsys)
    assert rc == 0
    assert json.loads((tmp_path / ".cairn" / "project.json").read_text())["agent_id"] == "grok-seat"
    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["cairn"]["env"]["CAIRN_AGENT"] == "grok-seat"
    # a cairn-cli-baked project warns loudly
    proj = json.loads((tmp_path / ".cairn" / "project.json").read_text())
    proj["agent_id"] = "cairn-cli"
    (tmp_path / ".cairn" / "project.json").write_text(json.dumps(proj))
    rc, out, err = run(["bootstrap", "--json"], capsys)
    assert rc == 0
    assert "cairn-cli" in json.loads(out)["warnings"][0] and "cairn-cli" in err


def test_human_retrieve_shows_full_content(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "human-full")
    assert run(["init", "--yes", "--embed-spec", "hash"], capsys)[0] == 0
    body = "human retrieve must show the whole memory " * 10  # ~430 chars
    assert run(["store", body, "--team", "t", "--task", "k"], capsys)[0] == 0
    rc, out, _ = run(["retrieve", "whole memory", "--task", "k"], capsys)
    assert rc == 0 and body in out
