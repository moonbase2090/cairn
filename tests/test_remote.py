import hashlib
import json

from cairn import cli
from cairn.client import CairnClient
from cairn.embed import HashEmbedder
from cairn.mcp_server import call_tool, make_client
from cairn.remote import RemoteConfig, remote_config
from cairn.serve import start_background
from cairn.store import Vault
from cairn.vault_identity import VaultIdentity


def _server_client(path):
    embedder = HashEmbedder()
    vault = Vault(path, embedder.name, embedder.dims, create=True)
    vault._set_meta("vault_id", "remote-cli-test-vault")
    vault._set_meta("vault_name", "remote-cli-test")
    vault.conn.commit()
    vault._vault_identity = VaultIdentity("remote-cli-test-vault", "remote-cli-test")
    return CairnClient(vault, "server", embedder)


def test_remote_config_uses_environment_pair_and_private_ca(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_URL", "https://vault.example")
    monkeypatch.delenv("CAIRN_TOKEN", raising=False)
    assert remote_config(tmp_path) is None

    monkeypatch.setenv("CAIRN_TOKEN", "secret-token")
    monkeypatch.setenv("CAIRN_TLS_CA", "/tmp/test-ca.pem")
    assert remote_config(tmp_path) == RemoteConfig(
        "https://vault.example", "secret-token", "/tmp/test-ca.pem",
    )


def test_remote_config_reads_vault_config(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_URL", raising=False)
    monkeypatch.delenv("CAIRN_TOKEN", raising=False)
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "config.toml").write_text(
        '[remote]\nurl = "https://vault.example"\ntoken = "config-token"\n'
    )

    assert remote_config(vault_dir) == RemoteConfig(
        "https://vault.example", "config-token",
    )


def test_cli_and_mcp_route_memory_operations_to_remote_vault(tmp_path, monkeypatch, capsys):
    server = _server_client(tmp_path / "server" / "vault.db")
    digest = hashlib.sha256(b"agent-token").hexdigest()
    with server.vault.transaction():
        server.vault.create_server_token("ct_agent", digest, "agent-a", 1)
    srv = start_background(server, token_mode="per-agent")
    local_dir = tmp_path / "client"
    try:
        monkeypatch.setenv("CAIRN_URL", f"http://127.0.0.1:{srv.server_address[1]}")
        monkeypatch.setenv("CAIRN_TOKEN", "agent-token")
        monkeypatch.setenv("CAIRN_AGENT", "agent-a")
        monkeypatch.setenv("CAIRN_DIR", str(local_dir))

        assert cli.main([
            "store", "A fact written through the remote client.",
            "--team", "team", "--task", "remote", "--mode", "new", "--json",
        ]) == 0
        stored = json.loads(capsys.readouterr().out)
        assert server.get_memory(stored["key"]).agent_id == "agent-a"
        assert not (local_dir / "vault.db").exists()

        client = make_client()
        result = json.loads(call_tool(client, "retrieve_memory", {
            "query": "fact written remote", "task_id": "remote",
        }))
        assert result[0]["key"] == stored["key"]
        assert result[0]["vault_id"] == "remote-cli-test-vault"
    finally:
        srv.shutdown()
