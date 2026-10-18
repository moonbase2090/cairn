"""[storage] backend selection from vault, generated AWS, and home settings."""
import json

import pytest

from cairn.cli import main, storage_config
from cairn.mcp_server import make_client
from cairn.storage import StorageConfig, UnknownBackendError


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    home, vdir = tmp_path / "home", tmp_path / ".cairn"
    (home / ".cairn").mkdir(parents=True)
    vdir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CAIRN_DIR", str(vdir))
    monkeypatch.setenv("CAIRN_AGENT", "config-test")
    return home / ".cairn" / "config.toml", vdir


def run(argv, capsys):
    rc = main(argv)
    out, err = capsys.readouterr()
    return rc, out, err


def test_default_is_sqlite(dirs):
    _, vdir = dirs
    assert storage_config(vdir) == StorageConfig()


def test_home_config_selects_backend(dirs):
    home_cfg, vdir = dirs
    home_cfg.write_text('[agent]\nid = "x"\n\n[storage]\nbackend = "from-home"\n')
    assert storage_config(vdir) == StorageConfig(backend="from-home")


def test_vault_config_wins_over_home(dirs):
    home_cfg, vdir = dirs
    home_cfg.write_text('[storage]\nbackend = "from-home"\n')
    (vdir / "config.toml").write_text('[storage]\nbackend = "sqlite"\n')
    assert storage_config(vdir) == StorageConfig()


def test_config_without_storage_table_falls_through(dirs):
    home_cfg, vdir = dirs
    (vdir / "config.toml").write_text('[agent]\nid = "x"\n')
    home_cfg.write_text('[storage]\nbackend = "from-home"\n')
    assert storage_config(vdir) == StorageConfig(backend="from-home")


def test_generated_aws_settings_fall_after_vault_and_before_home(dirs):
    home_cfg, vdir = dirs
    home_cfg.write_text('[storage]\nbackend = "from-home"\n')
    generated = vdir / "aws" / "storage.toml"
    generated.parent.mkdir()
    generated.write_text(
        '[storage]\nbackend = "aws"\nregion = "us-west-2"\n'
        'vault_id = "0123456789abcdef0123456789abcdef"\n'
        'vault_name = "work-vault"\n'
        'table = "cairn-memory"\ncache_table = "cairn-cache"\n'
        'content_bucket = "cairn-content"\nvector_bucket = "cairn-vectors"\n'
        'vector_index = "cairn-index"\n'
        'vector_index_arn = "arn:aws:s3vectors:us-west-2:123456789012:index/example"\n'
        'sync_endpoint = "https://abc123.execute-api.us-west-2.amazonaws.com"\n'
    )

    assert storage_config(vdir).backend == "aws"
    (vdir / "config.toml").write_text('[storage]\nbackend = "sqlite"\n')
    assert storage_config(vdir) == StorageConfig()


@pytest.mark.parametrize("body", [
    pytest.param("[storage\n", id="bad-toml"),
    pytest.param('storage = "sqlite"\n', id="not-a-table"),
    pytest.param("[storage]\nbackend = 3\n", id="not-a-string"),
])
def test_malformed_config_is_an_error(dirs, body):
    _, vdir = dirs
    (vdir / "config.toml").write_text(body)
    with pytest.raises(ValueError, match="config.toml"):
        storage_config(vdir)


def test_postgres_config_reads_connection_url(dirs):
    home_cfg, vdir = dirs
    home_cfg.write_text('[storage]\nbackend = "postgres"\nurl = "postgresql://localhost/cairn"\n')
    assert storage_config(vdir) == StorageConfig(
        backend="postgres", url="postgresql://localhost/cairn")


def test_postgres_config_requires_url(dirs):
    _, vdir = dirs
    (vdir / "config.toml").write_text('[storage]\nbackend = "postgres"\n')
    with pytest.raises(ValueError, match="url is required"):
        storage_config(vdir)


def test_aws_config_reads_resource_names_without_credentials(dirs):
    home_cfg, _ = dirs
    home_cfg.write_text(
        '[storage]\n'
        'backend = "aws"\n'
        'region = "us-west-2"\n'
        'profile = "work"\n'
        'vault_id = "0123456789abcdef0123456789abcdef"\n'
        'vault_name = "work-vault"\n'
        'table = "cairn-memory"\n'
        'cache_table = "cairn-cache"\n'
        'content_bucket = "cairn-content"\n'
        'vector_bucket = "cairn-vectors"\n'
        'vector_index = "cairn-index"\n'
        'vector_index_arn = "arn:aws:s3vectors:us-west-2:123456789012:index/example"\n'
        'sync_endpoint = "https://abc123.execute-api.us-west-2.amazonaws.com"\n'
    )
    assert storage_config(dirs[1]) == StorageConfig(
        backend="aws", region="us-west-2", profile="work",
        vault_id="0123456789abcdef0123456789abcdef", vault_name="work-vault",
        table="cairn-memory",
        cache_table="cairn-cache", content_bucket="cairn-content",
        vector_bucket="cairn-vectors", vector_index="cairn-index",
        vector_index_arn="arn:aws:s3vectors:us-west-2:123456789012:index/example",
        sync_endpoint="https://abc123.execute-api.us-west-2.amazonaws.com",
    )


def test_aws_sync_endpoint_requires_https_base_url(dirs):
    _, vdir = dirs
    (vdir / "config.toml").write_text(
        '[storage]\nbackend = "aws"\nregion = "us-west-2"\n'
        'vault_id = "0123456789abcdef0123456789abcdef"\n'
        'vault_name = "work-vault"\ntable = "memory"\ncache_table = "cache"\n'
        'content_bucket = "content"\nvector_bucket = "vectors"\n'
        'vector_index = "index"\nvector_index_arn = "arn:aws:s3vectors:us-west-2:123456789012:index/mock"\n'
        'sync_endpoint = "http://example.test"\n'
    )
    with pytest.raises(ValueError, match="HTTPS API base URL"):
        storage_config(vdir)


def test_aws_config_requires_all_deployed_resources(dirs):
    _, vdir = dirs
    (vdir / "config.toml").write_text('[storage]\nbackend = "aws"\n')
    with pytest.raises(ValueError, match="AWS backend requires region"):
        storage_config(vdir)


def test_aws_resource_setting_without_backend_is_an_error(dirs):
    _, vdir = dirs
    (vdir / "config.toml").write_text('[storage]\nregion = "us-west-2"\n')
    with pytest.raises(ValueError, match="region requires a backend"):
        storage_config(vdir)


def test_storage_url_without_backend_is_an_error(dirs):
    _, vdir = dirs
    (vdir / "config.toml").write_text('[storage]\nurl = "postgresql://localhost/cairn"\n')
    with pytest.raises(ValueError, match="url requires backend"):
        storage_config(vdir)


def test_postgres_connection_failure_is_a_cli_error(dirs, capsys):
    _, vdir = dirs
    (vdir / "config.toml").write_text(
        '[storage]\nbackend = "postgres"\nurl = "postgresql://localhost:1/cairn"\n'
    )
    rc, _, err = run(["doctor"], capsys)
    assert rc == 2
    assert "cannot connect to PostgreSQL storage" in err
    assert "Traceback" not in err


def test_init_and_doctor_report_sqlite(dirs, capsys):
    _, vdir = dirs
    (vdir / "config.toml").write_text('[storage]\nbackend = "sqlite"\n')
    assert run(["init", "--embed-spec", "hash"], capsys)[0] == 0
    rc, out, _ = run(["doctor", "--json"], capsys)
    assert rc == 0 and json.loads(out)["storage"] == "sqlite"


def test_init_refuses_unknown_backend(dirs, capsys):
    _, vdir = dirs
    (vdir / "config.toml").write_text('[storage]\nbackend = "nosuch"\n')
    rc, _, err = run(["init", "--embed-spec", "hash"], capsys)
    assert rc == 2
    assert "unknown storage backend 'nosuch'" in err and "sqlite" in err
    assert not (vdir / "vault.db").exists()


def test_commands_refuse_unknown_backend(dirs, capsys):
    _, vdir = dirs
    assert run(["init", "--embed-spec", "hash"], capsys)[0] == 0
    (vdir / "config.toml").write_text('[storage]\nbackend = "nosuch"\n')
    rc, _, err = run(["list", "--task", "k"], capsys)
    assert rc == 2 and "unknown storage backend 'nosuch'" in err


def test_mcp_server_uses_configured_backend(dirs, capsys):
    _, vdir = dirs
    assert run(["init", "--embed-spec", "hash"], capsys)[0] == 0
    client = make_client()
    try:
        assert client.vault.name == "sqlite"
    finally:
        client.vault.close()
    (vdir / "config.toml").write_text('[storage]\nbackend = "nosuch"\n')
    with pytest.raises(UnknownBackendError):
        make_client()
