"""Sync-server tests — real HTTP on localhost, ephemeral ports."""
import hashlib
import json
import shutil
import subprocess
import urllib.error
import urllib.request

import pytest

from cairn import cli
from cairn.client import CairnClient
from cairn.embed import HashEmbedder
from cairn.serve import pull_from, push_to, start_background, sync_handshake
from cairn.storage import MemoryQuery
from cairn.store import Vault
from cairn.vault_identity import VaultIdentity


def make_client(db_path, agent="test"):
    emb = HashEmbedder()
    vault = Vault(db_path, emb.name, emb.dims, create=True)
    vault._set_meta("vault_id", "shared-serve-test-vault")
    vault._set_meta("vault_name", "serve-test")
    vault.conn.commit()
    vault._vault_identity = VaultIdentity("shared-serve-test-vault", "serve-test")
    return CairnClient(vault, agent, emb)


def add_server_token(client, token_id, raw_token, agent_id, curator=False):
    digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    with client.vault.transaction():
        client.vault.create_server_token(token_id, digest, agent_id, 1, curator=curator)
    return digest


def test_push_pull_roundtrip(tmp_path):
    c1 = make_client(tmp_path / "a" / "vault.db", "claude-t")
    c1.store_memory("sync this fact across peers please", team_id="t", task_id="k")
    server_client = make_client(tmp_path / "srv" / "vault.db", "server")
    srv = start_background(server_client, token="s3cret")
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        with urllib.request.urlopen(url + "/health", timeout=5) as r:
            assert json.load(r)["ok"] is True
        out = push_to(url, c1.export(), token="s3cret")
        assert out == {"added": 1, "skipped": 0}
        pack = pull_from(url, token="s3cret")
        assert len(pack["memories"]) == 1
        c2 = make_client(tmp_path / "b" / "vault.db", "grok-t")
        assert c2.import_pack(pack) == {"added": 1, "skipped": 0}
        assert c2.retrieve_memory("sync fact", filters={"task_id": "k"})
    finally:
        srv.shutdown()


def test_server_rejects_push_that_skips_the_saved_peer_cursor(tmp_path):
    source = make_client(tmp_path / "a" / "vault.db", "source-agent")
    source.store_memory("A cursor gap must be rejected.", team_id="t", task_id="sync")
    pack = source.export_delta()
    pack["after"] = pack["cursor"]
    server = make_client(tmp_path / "server" / "vault.db", "server")
    srv = start_background(server)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        with pytest.raises(urllib.error.HTTPError) as rejected:
            push_to(url, pack)
        assert rejected.value.code == 409
        assert server.vault.count() == 0
    finally:
        srv.shutdown()


def test_sync_handshake_and_push_reject_a_different_vault(tmp_path):
    source = make_client(tmp_path / "source" / "vault.db", "source-agent")
    source.vault._set_meta("vault_id", "different-serve-vault")
    source.vault._set_meta("vault_name", "different")
    source.vault.conn.commit()
    source.vault._vault_identity = VaultIdentity("different-serve-vault", "different")
    source.store_memory("This pack must not cross vaults.", team_id="t", task_id="sync")

    server = make_client(tmp_path / "server" / "vault.db", "server-agent")
    srv = start_background(server)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        with pytest.raises(ValueError, match="different vault"):
            sync_handshake(url, source.vault.vault_identity.vault_id)
        with pytest.raises(urllib.error.HTTPError) as rejected:
            push_to(url, source.export_delta())
        assert rejected.value.code == 400
        assert server.vault.count() == 0
        assert server.vault.list_sync_cursors() == []
    finally:
        srv.shutdown()


def test_bad_token_rejected(tmp_path):
    server_client = make_client(tmp_path / "srv" / "vault.db", "server")
    srv = start_background(server_client, token="s3cret")
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        with pytest.raises(urllib.error.URLError):
            push_to(url, server_client.export(), token="wrong")
        with pytest.raises(urllib.error.URLError):
            pull_from(url, token="wrong")
    finally:
        srv.shutdown()


def test_per_agent_tokens_bind_pushes_and_allow_shared_rows_to_round_trip(tmp_path):
    source_a = make_client(tmp_path / "a" / "vault.db", "client-a")
    source_a.store_memory("memory from client a", team_id="t", task_id="a")
    source_b = make_client(tmp_path / "b" / "vault.db", "client-b")
    source_b.store_memory("memory from client b", team_id="t", task_id="b")
    server_client = make_client(tmp_path / "srv" / "vault.db", "server")
    token_a = "cairn-token-a"
    token_b = "cairn-token-b"
    digest_a = add_server_token(server_client, "ct_a", token_a, "client-a")
    add_server_token(server_client, "ct_b", token_b, "client-b")
    srv = start_background(server_client, token_mode="per-agent")
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"

        # A token cannot create memories owned by another agent.
        with pytest.raises(urllib.error.HTTPError) as mismatch:
            push_to(url, source_a.export(), token=token_b)
        assert mismatch.value.code == 403
        assert server_client.vault.count() == 0

        forged_identity = source_b.export()
        forged_identity["memories"][0]["agent_id"] = "client-a"
        with pytest.raises(urllib.error.HTTPError) as forged_key:
            push_to(url, forged_identity, token=token_a)
        assert forged_key.value.code == 403
        assert server_client.vault.count() == 0

        # Unknown credentials cannot read or write.
        with pytest.raises(urllib.error.HTTPError) as rejected_push:
            push_to(url, source_a.export(), token="unknown")
        assert rejected_push.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as rejected_pull:
            pull_from(url, token="unknown")
        assert rejected_pull.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as missing_auth:
            urllib.request.urlopen(url + "/pull", timeout=5)
        assert missing_auth.value.code == 401

        assert push_to(url, source_a.export(), token=token_a) == {"added": 1, "skipped": 0}
        assert push_to(url, source_b.export(), token=token_b) == {"added": 1, "skipped": 0}
        rows = server_client.vault.find(MemoryQuery())
        assert {row["agent_id"] for row in rows} == {"client-a", "client-b"}

        # Pulling the shared vault adds the other agent's rows locally. Those
        # server-owned rows can safely round-trip on the next push.
        pulled = pull_from(url, token=token_b)
        assert source_b.import_pack(pulled) == {"added": 1, "skipped": 1}
        assert push_to(url, source_b.export(), token=token_b) == {"added": 0, "skipped": 2}

        with server_client.vault.transaction():
            assert server_client.vault.delete_server_token("ct_a") == 1
        with pytest.raises(urllib.error.HTTPError) as revoked:
            pull_from(url, token=token_a)
        assert revoked.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as revoked_push:
            push_to(url, source_a.export(), token=token_a)
        assert revoked_push.value.code == 401
        assert server_client.vault.get_server_token(digest_a) is None
    finally:
        srv.shutdown()


def test_curator_token_can_apply_cross_agent_state_events(tmp_path):
    owner = make_client(tmp_path / "owner" / "vault.db", "owner-agent")
    row = owner.store_memory("A fact owned by agent A.", team_id="t", task_id="curator")
    peer = make_client(tmp_path / "peer" / "vault.db", "peer-agent")
    server = make_client(tmp_path / "server" / "vault.db", "server-agent")
    add_server_token(server, "ct_owner", "owner-token", "owner-agent")
    add_server_token(server, "ct_peer", "peer-token", "peer-agent")
    add_server_token(server, "ct_curator", "curator-token", "peer-agent", curator=True)
    srv = start_background(server, token_mode="per-agent")
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        push_to(url, owner.export_delta(), token="owner-token")
        pack = pull_from(url, token="peer-token", after=0, peer=peer.vault.sync_origin_id())
        peer.import_sync_pack(pack)
        correction = peer.store_memory(
            "A correction owned by agent B.", team_id="t", task_id="curator",
            supersedes_key=row.key, mode="new",
        )
        attempted = peer.export_delta()

        with pytest.raises(urllib.error.HTTPError) as denied:
            push_to(url, attempted, token="peer-token")
        assert denied.value.code == 403
        assert server.get_memory(row.key).status == "active"
        with pytest.raises(urllib.error.HTTPError) as denied_v1:
            push_to(url, peer.export(), token="peer-token")
        assert denied_v1.value.code == 403

        # The curator may add a correction against another agent's row.
        assert push_to(url, peer.export(), token="curator-token")["added"] == 1
        accepted = push_to(url, attempted, token="curator-token")
        assert accepted["updated"] == 2
        assert server.get_memory(row.key).status == "superseded"
        assert server.get_memory(correction.key).status == "active"
    finally:
        srv.shutdown()


def test_per_agent_mode_still_requires_tls_off_loopback(tmp_path):
    server_client = make_client(tmp_path / "srv" / "vault.db", "server")
    with pytest.raises(ValueError, match="plain HTTP"):
        start_background(server_client, host="0.0.0.0", token_mode="per-agent")


def test_remote_http_is_refused(tmp_path):
    server_client = make_client(tmp_path / "srv" / "vault.db", "server")
    with pytest.raises(ValueError, match="plain HTTP"):
        start_background(server_client, host="0.0.0.0", token="s3cret")
    with pytest.raises(ValueError, match="https://"):
        push_to("http://203.0.113.10:8778", {"memories": []}, token="s3cret")


def _localhost_cert(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is required to mint a test certificate")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "1",
         "-nodes", "-keyout", str(key), "-out", str(cert),
         "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, capture_output=True,
    )
    return cert, key


def test_https_push_pull(tmp_path):
    cert, key = _localhost_cert(tmp_path)
    source = make_client(tmp_path / "a" / "vault.db", "claude-t")
    source.store_memory("sync this fact over tls please", team_id="t", task_id="k")
    server_client = make_client(tmp_path / "srv" / "vault.db", "server")
    srv = start_background(server_client, token="s3cret", tls_cert=cert, tls_key=key)
    try:
        url = f"https://127.0.0.1:{srv.server_address[1]}"
        out = push_to(url, source.export(), token="s3cret", cafile=str(cert))
        assert out == {"added": 1, "skipped": 0}
        pack = pull_from(url, token="s3cret", cafile=str(cert))
        assert len(pack["memories"]) == 1
    finally:
        srv.shutdown()


def test_https_per_agent_push_preserves_identity(tmp_path):
    cert, key = _localhost_cert(tmp_path)
    source = make_client(tmp_path / "a" / "vault.db", "client-a")
    source.store_memory("per-agent identity over tls", team_id="t", task_id="k")
    server_client = make_client(tmp_path / "srv" / "vault.db", "server")
    token = "cairn-tls-client-a"
    add_server_token(server_client, "ct_tls", token, "client-a")
    srv = start_background(
        server_client, host="0.0.0.0", token_mode="per-agent", tls_cert=cert, tls_key=key,
    )
    try:
        url = f"https://127.0.0.1:{srv.server_address[1]}"
        assert push_to(url, source.export(), token=token, cafile=str(cert)) == {
            "added": 1, "skipped": 0,
        }
        (row,) = server_client.vault.find(MemoryQuery())
        assert row["agent_id"] == "client-a"
    finally:
        srv.shutdown()


def test_cli_serve_uses_environment_token_without_printing_it(tmp_path, monkeypatch, capsys):
    server_client = make_client(tmp_path / "srv" / "vault.db", "server")
    monkeypatch.setenv("CAIRN_TOKEN", "environment-secret")
    calls = []
    print_kwargs = []

    def capture_startup(*args, **kwargs):
        print_kwargs.append(kwargs)
        print(*args, **kwargs)

    monkeypatch.setattr(cli, "serve_forever", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(cli, "print", capture_startup, raising=False)

    args = cli.build_parser().parse_args([
        "--vault", str(tmp_path / "srv"), "serve", "--host", "127.0.0.1", "--port", "8778",
    ])

    assert cli._cmd_serve(args, server_client) == 0
    assert calls[0][0][3] == "environment-secret"
    assert calls[0][1] == {"token_mode": "shared"}
    output = capsys.readouterr().out
    assert "bearer token configured" in output
    assert "environment-secret" not in output
    assert print_kwargs == [{"flush": True}]


def test_cli_serve_token_flag_overrides_environment(tmp_path, monkeypatch, capsys):
    server_client = make_client(tmp_path / "srv" / "vault.db", "server")
    monkeypatch.setenv("CAIRN_TOKEN", "environment-secret")
    calls = []
    monkeypatch.setattr(cli, "serve_forever", lambda *args, **kwargs: calls.append((args, kwargs)))

    args = cli.build_parser().parse_args([
        "--vault", str(tmp_path / "srv"), "serve", "--host", "127.0.0.1",
        "--port", "8778", "--token", "flag-secret",
    ])

    assert cli._cmd_serve(args, server_client) == 0
    assert calls[0][0][3] == "flag-secret"
    assert calls[0][1] == {"token_mode": "shared"}
    output = capsys.readouterr().out
    assert "bearer token configured" in output
    assert "flag-secret" not in output
    assert "environment-secret" not in output


def test_cli_serve_per_agent_mode_does_not_generate_a_shared_token(tmp_path, monkeypatch, capsys):
    server_client = make_client(tmp_path / "srv" / "vault.db", "server")
    calls = []
    monkeypatch.delenv("CAIRN_TOKEN", raising=False)
    monkeypatch.setattr(cli, "serve_forever", lambda *args, **kwargs: calls.append((args, kwargs)))

    args = cli.build_parser().parse_args([
        "--vault", str(tmp_path / "srv"), "serve", "--token-mode", "per-agent",
    ])

    assert cli._cmd_serve(args, server_client) == 0
    assert calls[0][0][3] == ""
    assert calls[0][1] == {"token_mode": "per-agent"}
    output = capsys.readouterr().out
    assert "per-agent tokens enabled" in output
    assert "token:" not in output


def test_cli_serve_per_agent_rejects_shared_token_config(tmp_path, monkeypatch):
    server_client = make_client(tmp_path / "srv" / "vault.db", "server")
    monkeypatch.setenv("CAIRN_TOKEN", "legacy-token")
    args = cli.build_parser().parse_args([
        "--vault", str(tmp_path / "srv"), "serve", "--token-mode", "per-agent",
    ])

    with pytest.raises(ValueError, match="omit --token and CAIRN_TOKEN"):
        cli._cmd_serve(args, server_client)


def test_cli_token_create_list_revoke(tmp_path, monkeypatch, capsys):
    client = make_client(tmp_path / "vault", "server")
    monkeypatch.setattr(cli, "_open_client", lambda _args: client)

    assert cli.main([
        "--vault", str(tmp_path / "vault"), "token", "create", "--agent", "client-a", "--json",
    ]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["agent_id"] == "client-a"
    assert created["token_id"].startswith("ct_")
    assert created["token"].startswith("cairn_")
    digest = hashlib.sha256(created["token"].encode()).hexdigest()
    stored_hash = client.vault.conn.execute(
        "SELECT token_hash FROM server_tokens WHERE token_id=?", (created["token_id"],)
    ).fetchone()["token_hash"]
    assert stored_hash == digest
    assert stored_hash != created["token"]

    assert cli.main(["--vault", str(tmp_path / "vault"), "token", "list", "--json"]) == 0
    listed_output = capsys.readouterr().out
    listed = json.loads(listed_output)
    assert listed == [{
        "token_id": created["token_id"], "agent_id": "client-a", "curator": False,
        "created_at": listed[0]["created_at"],
    }]
    assert created["token"] not in listed_output

    assert cli.main([
        "--vault", str(tmp_path / "vault"), "token", "revoke", created["token_id"], "--json",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "token_id": created["token_id"], "revoked": True,
    }
    assert client.vault.get_server_token(digest) is None
    assert cli.main(["--vault", str(tmp_path / "vault"), "token", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert cli.main([
        "--vault", str(tmp_path / "vault"), "token", "revoke", created["token_id"], "--json",
    ]) == 2
    assert "no active token" in capsys.readouterr().err

    assert cli.main([
        "--vault", str(tmp_path / "vault"), "token", "create", "--agent", " ", "--json",
    ]) == 2
    assert "--agent must not be empty" in capsys.readouterr().err
