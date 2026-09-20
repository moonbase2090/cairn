"""Sync-server tests — real HTTP on localhost, ephemeral ports."""
import json
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
