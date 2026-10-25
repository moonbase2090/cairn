import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cairn import cli
from cairn.client import CairnClient
from cairn.embed import HashEmbedder
from cairn.mcp_server import call_tool, make_client
from cairn.remote import RemoteCairnClient, RemoteConfig, remote_config
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
    monkeypatch.setenv("CAIRN_REMOTE_URL", "https://vault.example")
    monkeypatch.delenv("CAIRN_REMOTE_TOKEN", raising=False)
    assert remote_config(tmp_path) is None

    monkeypatch.setenv("CAIRN_REMOTE_TOKEN", "secret-token")
    monkeypatch.setenv("CAIRN_REMOTE_TLS_CA", "/tmp/test-ca.pem")
    assert remote_config(tmp_path) == RemoteConfig(
        "https://vault.example",
        "secret-token",
        "/tmp/test-ca.pem",
    )


def test_remote_config_reads_vault_config(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_REMOTE_URL", raising=False)
    monkeypatch.delenv("CAIRN_REMOTE_TOKEN", raising=False)
    monkeypatch.setenv("CAIRN_URL", "https://sync.example")
    monkeypatch.setenv("CAIRN_TOKEN", "sync-token")
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "config.toml").write_text(
        '[remote]\nurl = "https://vault.example"\ntoken = "config-token"\n'
    )

    assert remote_config(vault_dir) == RemoteConfig(
        "https://vault.example",
        "config-token",
    )


def test_cli_reports_invalid_remote_config_as_a_managed_error(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path))
    monkeypatch.delenv("CAIRN_REMOTE_URL", raising=False)
    monkeypatch.delenv("CAIRN_REMOTE_TOKEN", raising=False)
    (tmp_path / "config.toml").write_text('remote = "invalid"\n')

    assert cli.main(["whoami"]) == 2
    assert (
        capsys.readouterr().err.strip()
        == "error: config.toml: [remote] must be a table"
    )


def test_sync_environment_keeps_cli_and_mcp_memory_operations_local(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CAIRN_REMOTE_URL", raising=False)
    monkeypatch.delenv("CAIRN_REMOTE_TOKEN", raising=False)
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / "local"))
    monkeypatch.setenv("CAIRN_AGENT", "agent-a")
    assert cli.main(["init", "--yes", "--embed-spec", "hash"]) == 0
    capsys.readouterr()
    server = _server_client(tmp_path / "server" / "vault.db")
    srv = start_background(server, token="shared-sync-token")
    try:
        monkeypatch.setenv("CAIRN_URL", f"http://127.0.0.1:{srv.server_address[1]}")
        monkeypatch.setenv("CAIRN_TOKEN", "shared-sync-token")
        assert (
            cli.main(
                [
                    "store",
                    "A local fact with sync configured.",
                    "--team",
                    "team",
                    "--task",
                    "local",
                    "--json",
                ]
            )
            == 0
        )
        stored = json.loads(capsys.readouterr().out)
        assert cli.main(["whoami", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["storage"] == "sqlite"
        assert cli.main(["retrieve", "local fact", "--task", "local", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)[0]["key"] == stored["key"]
        result = json.loads(
            call_tool(
                make_client(),
                "retrieve_memory",
                {"query": "local fact", "task_id": "local"},
            )
        )
        assert result[0]["key"] == stored["key"]
        assert server.list_memories({"task_id": "local"}) == []
    finally:
        srv.shutdown()


def test_remote_client_does_not_forward_token_to_redirect_target():
    target_requests = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            target_requests.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    target_thread.start()

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{target.server_address[1]}/rpc"
            )
            self.end_headers()

        def log_message(self, *args):
            pass

    redirector = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    redirect_thread = threading.Thread(target=redirector.serve_forever, daemon=True)
    redirect_thread.start()
    client = RemoteCairnClient.__new__(RemoteCairnClient)
    client.config = RemoteConfig(
        f"http://127.0.0.1:{redirector.server_address[1]}", "secret-token"
    )
    try:
        try:
            client._request("whoami", {})
        except ValueError as exc:
            assert "remote request failed" in str(exc)
        else:
            raise AssertionError("redirected remote request unexpectedly succeeded")
        assert target_requests == []
    finally:
        redirector.shutdown()
        redirector.server_close()
        redirect_thread.join(timeout=2)
        target.shutdown()
        target.server_close()
        target_thread.join(timeout=2)


def test_cli_and_mcp_route_memory_operations_to_remote_vault(
    tmp_path, monkeypatch, capsys
):
    server = _server_client(tmp_path / "server" / "vault.db")
    digest = hashlib.sha256(b"agent-token").hexdigest()
    with server.vault.transaction():
        server.vault.create_server_token("ct_agent", digest, "agent-a", 1)
    srv = start_background(server, token_mode="per-agent")
    local_dir = tmp_path / "client"
    try:
        monkeypatch.setenv(
            "CAIRN_REMOTE_URL", f"http://127.0.0.1:{srv.server_address[1]}"
        )
        monkeypatch.setenv("CAIRN_REMOTE_TOKEN", "agent-token")
        monkeypatch.setenv("CAIRN_AGENT", "agent-a")
        monkeypatch.setenv("CAIRN_DIR", str(local_dir))

        assert (
            cli.main(
                [
                    "store",
                    "A fact written through the remote client.",
                    "--team",
                    "team",
                    "--task",
                    "remote",
                    "--mode",
                    "new",
                    "--json",
                ]
            )
            == 0
        )
        stored = json.loads(capsys.readouterr().out)
        assert server.get_memory(stored["key"]).agent_id == "agent-a"
        assert not (local_dir / "vault.db").exists()

        client = make_client()
        result = json.loads(
            call_tool(
                client,
                "retrieve_memory",
                {
                    "query": "fact written remote",
                    "task_id": "remote",
                },
            )
        )
        assert result[0]["key"] == stored["key"]
        assert result[0]["vault_id"] == "remote-cli-test-vault"
    finally:
        srv.shutdown()
