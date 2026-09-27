"""cairn-embedd socket protocol — hash backend, no FastEmbed load."""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from cairn.embed import HashEmbedder
from cairn.embedd import (
    DARWIN_SUN_PATH, SocketEmbedder, check_sock_path, ping, sock_path, spawn,
    spawn_lock,
)

ROOT = Path(__file__).resolve().parents[1]


def _wait_sock(path: Path, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ping(path):
            return
        time.sleep(0.05)
    raise AssertionError(f"embedd did not listen on {path}")


@pytest.fixture
def sock_dir():
    """A short socket dir: pytest's macOS tmp_path overflows sun_path."""
    path = Path(tempfile.mkdtemp(prefix="cairn-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def embedd_hash(sock_dir):
    sock = sock_dir / "embed.sock"
    proc = subprocess.Popen(
        [sys.executable, "-m", "cairn.embedd", "--spec", "hash",
         "--sock", str(sock), "--idle", "8", "--dims", "384"],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": f"{ROOT}/src",
             "CAIRN_EMBEDD": "0"},  # daemon loads in-process hash
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_sock(sock)
        yield sock
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_socket_embed_matches_local_hash(embedd_hash):
    local = HashEmbedder(dims=384)
    remote = SocketEmbedder("hash", path=embedd_hash, dims=384)
    texts = ["coolant manifold pressure", "how to use shared memory"]
    a = local.embed(texts)
    b = remote.embed(texts)
    assert a.shape == b.shape == (2, 384)
    assert np.allclose(a, b, atol=1e-6)
    info = remote.info()
    assert info["name"] == "hash-v2" and info["dims"] == 384


def test_ping_false_when_missing(tmp_path):
    assert ping(tmp_path / "nope.sock") is False


def test_idle_exit(sock_dir):
    sock = sock_dir / "idle.sock"
    proc = subprocess.Popen(
        [sys.executable, "-m", "cairn.embedd", "--spec", "hash",
         "--sock", str(sock), "--idle", "1", "--dims", "384"],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": f"{ROOT}/src",
             "CAIRN_EMBEDD": "0"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_sock(sock)
    SocketEmbedder("hash", path=sock).info()
    deadline = time.time() + 5
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    assert proc.poll() is not None, "embedd should idle-exit"
    assert ping(sock) is False


def test_spawn_lock_is_exclusive(tmp_path):
    sock = tmp_path / "embed.sock"
    order = []

    def hold():
        with spawn_lock(sock):
            order.append("hold")
            time.sleep(0.2)
            order.append("release")

    thread = threading.Thread(target=hold)
    thread.start()
    time.sleep(0.05)
    with spawn_lock(sock):
        order.append("next")
    thread.join()
    assert order == ["hold", "release", "next"]


def test_sock_path_prefers_xdg_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("CAIRN_EMBED_SOCK", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert sock_path() == tmp_path / "cairn" / "embed.sock"


def test_sock_path_macos_uses_per_user_temp(monkeypatch, tmp_path):
    monkeypatch.delenv("CAIRN_EMBED_SOCK", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    assert sock_path() == tmp_path / f"cairn-{os.getuid()}" / "embed.sock"


def test_sock_path_linux_uses_run_user(monkeypatch):
    monkeypatch.delenv("CAIRN_EMBED_SOCK", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert sock_path() == Path(f"/run/user/{os.getuid()}") / "cairn" / "embed.sock"


def test_check_sock_path_rejects_long_macos_path(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    check_sock_path(Path("/" + "x" * (DARWIN_SUN_PATH - 2)))
    with pytest.raises(OSError, match="CAIRN_EMBED_SOCK"):
        check_sock_path(Path("/" + "x" * (DARWIN_SUN_PATH - 1)))


def test_check_sock_path_leaves_linux_alone(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    check_sock_path(Path("/" + "x" * 200))


def test_spawn_fails_fast_on_long_macos_path(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    sock = tmp_path / ("x" * DARWIN_SUN_PATH) / "embed.sock"
    start = time.monotonic()
    with pytest.raises(OSError):
        spawn("hash", sock)
    assert time.monotonic() - start < 1
    assert not sock.parent.exists()
