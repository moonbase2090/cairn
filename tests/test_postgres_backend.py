"""PostgreSQL integration tests run against the pgvector CI service."""
from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from cairn.cli import main
from cairn.client import CairnClient
from cairn.embed import get_embedder
from cairn.serve import pull_from, push_to, start_background
from cairn.storage import StorageConfig, open_backend


@pytest.fixture
def postgres_url():
    url = os.environ.get("CAIRN_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("CAIRN_TEST_POSTGRES_URL is not set")
    import psycopg
    from psycopg.conninfo import make_conninfo
    from psycopg.sql import SQL, Identifier

    schema = f"cairn_test_{uuid.uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
    test_url = make_conninfo(url, options=f"-c search_path={schema},public")
    yield test_url
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))


def test_concurrent_postgres_initializers_converge(tmp_path, postgres_url):
    config = StorageConfig(backend="postgres", url=postgres_url)
    embedder = get_embedder("hash", dims=8)

    def initialize():
        return open_backend(
            tmp_path / "vault", embedder.name, 8, config=config, create=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(initialize) for _ in range(2)]
        vaults = [future.result() for future in futures]
    try:
        writer = CairnClient(vaults[0], "writer", embedder)
        writer.store_memory(
            "created after two initializers", "team", "task", mode="new",
            vector=np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=np.float32),
        )
        assert vaults[1].count() == 1
    finally:
        for vault in vaults:
            vault.close()


def test_two_serve_instances_share_postgres(tmp_path, postgres_url):
    config = StorageConfig(backend="postgres", url=postgres_url)
    embedder = get_embedder("hash", dims=8)
    vault_a = open_backend(tmp_path / "node-a", embedder.name, 8, config=config,
                           create=True, doc_threshold=10)
    vault_b = open_backend(tmp_path / "node-b", embedder.name, 8, config=config)
    client_a = CairnClient(vault_a, "agent-a", embedder)
    client_b = CairnClient(vault_b, "agent-b", embedder)
    server_a = start_background(client_a, port=0)
    server_b = start_background(client_b, port=0)
    url_a = f"http://127.0.0.1:{server_a.server_address[1]}"
    url_b = f"http://127.0.0.1:{server_b.server_address[1]}"
    vectors = [
        np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=np.float32),
        np.array([0, 1, 0, 0, 0, 0, 0, 0], dtype=np.float32),
    ]
    sources = []
    keys = []
    packs = []
    try:
        for index, vector in enumerate(vectors):
            source_vault = open_backend(
                tmp_path / f"source-{index}", embedder.name, 8,
                config=StorageConfig(), create=True, doc_threshold=10,
            )
            sources.append(source_vault)
            source_client = CairnClient(source_vault, f"source-{index}", embedder)
            result = source_client.store_memory(
                f"written by server {index}", "team", "shared", mode="new", vector=vector,
            )
            keys.append(result.key)
            packs.append(source_client.export())
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(push_to, url_a, packs[0]),
                pool.submit(push_to, url_b, packs[1]),
            ]
            results = [future.result() for future in futures]
        assert [result["added"] for result in results] == [1, 1]
        pack_a = pull_from(url_a)
        pack_b = pull_from(url_b)
        assert {row["key"] for row in pack_a["memories"]} == set(keys)
        assert {row["key"] for row in pack_b["memories"]} == set(keys)

        local_vault = open_backend(
            tmp_path / "sqlite-destination", embedder.name, 8,
            config=StorageConfig(), create=True, doc_threshold=10,
        )
        try:
            local_client = CairnClient(local_vault, "local", embedder)
            assert local_client.import_pack(pack_a) == {"added": 2, "skipped": 0}
            assert [local_client.get_memory(key).content for key in keys] == [
                "written by server 0", "written by server 1",
            ]
        finally:
            local_vault.close()
    finally:
        server_a.shutdown()
        server_b.shutdown()
        server_a.server_close()
        server_b.server_close()
        vault_a.close()
        vault_b.close()
        for source in sources:
            source.close()


def test_cli_and_mcp_open_postgres_from_config(
    tmp_path, monkeypatch, capsys, postgres_url,
):
    home = tmp_path / "home"
    vdir = tmp_path / ".cairn"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CAIRN_DIR", str(vdir))
    monkeypatch.setenv("CAIRN_AGENT", "postgres-test")
    vdir.mkdir()
    (vdir / "config.toml").write_text(
        f'[storage]\nbackend = "postgres"\nurl = "{postgres_url}"\n'
    )

    assert main(["init", "--embed-spec", "hash"]) == 0
    capsys.readouterr()
    assert main(["bootstrap", "--no-seed"]) == 0
    capsys.readouterr()
    assert main(["init", "--embed-spec", "hash"]) == 0
    capsys.readouterr()
    client = None
    try:
        from cairn.mcp_server import make_client

        client = make_client()
        assert client.vault.name == "postgres"
        assert not (vdir / "vault.db").exists()
        assert main(["doctor", "--json"]) == 0
        output, _ = capsys.readouterr()
        doctor = json.loads(output)
        assert doctor["storage"] == "postgres"
        assert "vault_mb" not in doctor
    finally:
        if client is not None:
            client.vault.close()
