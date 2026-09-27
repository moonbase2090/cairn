"""Sync-server tests — real HTTP on localhost, ephemeral ports."""
import json
import shutil
import subprocess
import urllib.error
import urllib.request

import pytest

from cairn.client import CairnClient
from cairn.embed import HashEmbedder
from cairn.serve import pull_from, push_to, start_background
from cairn.store import Vault


def make_client(db_path, agent="test"):
    emb = HashEmbedder()
    return CairnClient(Vault(db_path, emb.name, emb.dims, create=True), agent, emb)


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
