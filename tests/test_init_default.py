"""Init-default tests — fastembed-first with hash fallback. Pass in any env."""
import json

import cairn.cli as cli_mod
from cairn.cli import main


def run(argv, capsys):
    rc = main(argv)
    return rc, *capsys.readouterr()


def test_init_defaults_to_fastembed_when_available(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "init-test")
    rc, out, _ = run(["init", "--json"], capsys)
    assert rc == 0
    body = json.loads(out)
    # either fastembed (extra installed) or hash (fallback) — but never an error,
    # and the hint file always agrees with the vault
    assert body["embedder"] in ("fastembed-bge-small", "hash-v2")
    assert (tmp_path / ".cairn" / "embedder").read_text().splitlines()[0].strip() in ("fastembed", "hash")
    gi = (tmp_path / ".cairn" / ".gitignore").read_text()
    assert "vault.db-wal" in gi and "vault.db-shm" in gi
    rc, out, _ = run(["whoami", "--json"], capsys)
    assert json.loads(out)["embedder"] == body["embedder"]


def test_init_falls_back_to_hash_with_notice(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "init-test")
    real = cli_mod.get_embedder

    def no_fastembed(spec, dims=None, skip_socket=False):
        if spec == "fastembed" or spec.startswith("fastembed:"):
            raise ImportError("fastembed is not installed")
        return real(spec, dims, skip_socket=skip_socket)

    monkeypatch.setattr(cli_mod, "get_embedder", no_fastembed)
    rc, out, _ = run(["init", "--json"], capsys)
    assert rc == 0
    body = json.loads(out)
    assert body["embedder"] == "hash-v2"
    assert "notice" in body and "fastembed" in body["notice"]
    assert (tmp_path / ".cairn" / "embedder").read_text().splitlines()[0].strip() == "hash"


def test_init_explicit_spec_is_hard_error(tmp_path, monkeypatch, capsys):
    """An explicit `--embed-spec fastembed:…` without the package fails loudly —
    only the bare default gets the soft landing to hash."""
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "init-test")
    real = cli_mod.get_embedder

    def no_fastembed(spec, dims=None, skip_socket=False):
        if spec == "fastembed" or spec.startswith("fastembed:"):
            raise ImportError("fastembed is not installed")
        return real(spec, dims, skip_socket=skip_socket)

    monkeypatch.setattr(cli_mod, "get_embedder", no_fastembed)
    rc, _, err = run(["init", "--embed-spec", "fastembed:custom-model"], capsys)
    assert rc == 2 and "fastembed" in err
