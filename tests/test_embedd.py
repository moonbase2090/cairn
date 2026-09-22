"""cairn-embedd socket protocol — hash backend, no FastEmbed load."""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from cairn.embed import HashEmbedder
from cairn.embedd import SocketEmbedder, ping, spawn_lock

ROOT = Path(__file__).resolve().parents[1]


def _wait_sock(path: Path, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ping(path):
            return
        time.sleep(0.05)
    raise AssertionError(f"embedd did not listen on {path}")


@pytest.fixture
def embedd_hash(tmp_path):
    sock = tmp_path / "embed.sock"
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


def test_idle_exit(tmp_path):
    sock = tmp_path / "idle.sock"
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
