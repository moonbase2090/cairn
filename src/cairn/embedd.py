"""cairn-embedd — one ONNX session for the machine.

Unix socket at $XDG_RUNTIME_DIR/cairn/embed.sock (override $CAIRN_EMBED_SOCK).
CLI and cairn-mcp send texts; this process holds FastEmbed. Hash stays
in-process on the client. This is not `cairn serve` (that is per-vault pack
sync). Vaults still store memories.embedding BLOB + sqlite-vec; this daemon
shares the model only.

Protocol: 4-byte big-endian length + UTF-8 JSON.
  -> {"op":"embed","spec":"fastembed","texts":["…"]}
  <- {"ok":true,"name":"fastembed-bge-small","dims":384,"vectors":["<b64 float32>",…]}
  -> {"op":"ping"|"info"}
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import socket
import struct
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np

HDR = struct.Struct("!I")
DEFAULT_IDLE = 900  # 15 minutes


def sock_path() -> Path:
    env = os.environ.get("CAIRN_EMBED_SOCK")
    if env:
        return Path(env)
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "cairn" / "embed.sock"


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("embedd peer closed")
        buf.extend(chunk)
    return bytes(buf)


def recv_msg(conn: socket.socket) -> dict:
    raw_len = _recv_exact(conn, HDR.size)
    (n,) = HDR.unpack(raw_len)
    if n > 32 * 1024 * 1024:
        raise ValueError("embedd message too large")
    return json.loads(_recv_exact(conn, n))


def send_msg(conn: socket.socket, obj: dict) -> None:
    body = json.dumps(obj, separators=(",", ":")).encode()
    conn.sendall(HDR.pack(len(body)) + body)


def encode_vectors(arr: np.ndarray) -> list[str]:
    out = []
    for row in np.asarray(arr, dtype=np.float32):
        out.append(base64.b64encode(row.tobytes()).decode("ascii"))
    return out


def decode_vectors(rows: list[str]) -> np.ndarray:
    vecs = [np.frombuffer(base64.b64decode(r), dtype=np.float32).copy() for r in rows]
    return np.stack(vecs).astype(np.float32)


class SocketEmbedder:
    """Client for cairn-embedd. name/dims filled on first embed or info()."""

    name = "socket"
    dims = 0

    def __init__(self, spec: str, path: Path | None = None, dims: int | None = None):
        self.spec = spec
        self.path = path or sock_path()
        if dims:
            self.dims = int(dims)

    def _call(self, payload: dict) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(60)
            conn.connect(str(self.path))
            send_msg(conn, payload)
            reply = recv_msg(conn)
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error") or "embedd error")
        if reply.get("name"):
            self.name = reply["name"]
        if reply.get("dims"):
            self.dims = int(reply["dims"])
        return reply

    def info(self) -> dict:
        return self._call({"op": "info"})

    def embed(self, texts: list[str]) -> np.ndarray:
        reply = self._call({"op": "embed", "spec": self.spec, "texts": list(texts)})
        return decode_vectors(reply["vectors"])


def ping(path: Path | None = None) -> bool:
    path = path or sock_path()
    try:
        SocketEmbedder("ping", path=path)._call({"op": "ping"})
        return True
    except OSError:
        return False


def lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


@contextmanager
def spawn_lock(path: Path):
    """Exclusive lock so two clients cannot unlink each other's socket."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path(path), "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def spawn(spec: str, path: Path | None = None, idle: int = DEFAULT_IDLE) -> None:
    """Start cairn-embedd in a new session if the socket is dead."""
    path = path or sock_path()
    with spawn_lock(path):
        if ping(path):
            return
        cmd = [sys.executable, "-m", "cairn.embedd", "--spec", spec,
               "--sock", str(path), "--idle", str(idle)]
        import subprocess
        subprocess.Popen(
            cmd,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if ping(path):
                return
            time.sleep(0.1)
        raise RuntimeError(f"cairn-embedd did not come up at {path}")


def _op_embed(req, embedder, lock) -> None:
    texts = req.get("texts") or []
    if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
        return {"ok": False, "error": "texts must be a string list"}
    with lock:
        arr = embedder.embed(texts)
    return {
        "ok": True, "name": embedder.name, "dims": embedder.dims,
        "vectors": encode_vectors(arr),
    }


def _dispatch_op(req, embedder, lock) -> dict:
    op = req.get("op")
    if op == "ping":
        return {"ok": True}
    if op == "info":
        return {"ok": True, "name": embedder.name, "dims": embedder.dims}
    if op == "embed":
        return _op_embed(req, embedder, lock)
    return {"ok": False, "error": f"unknown op {op!r}"}


def _close_conn(conn: socket.socket) -> None:
    try:
        conn.close()
    except OSError:
        pass


def _handle(conn: socket.socket, embedder, lock: threading.Lock, last: list) -> None:
    try:
        req = recv_msg(conn)
        last[0] = time.monotonic()
        send_msg(conn, _dispatch_op(req, embedder, lock))
    except (OSError, ValueError, json.JSONDecodeError):
        return
    finally:
        _close_conn(conn)


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _accept_until_idle(srv, path, embedder, lock, last, idle: int) -> None:
    try:
        while True:
            if idle > 0 and time.monotonic() - last[0] > idle:
                break
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            conn.settimeout(60)
            threading.Thread(
                target=_handle, args=(conn, embedder, lock, last), daemon=True,
            ).start()
    finally:
        srv.close()
        _unlink_quiet(path)


def serve_forever(spec: str, path: Path | None = None, idle: int = DEFAULT_IDLE,
                  dims: int | None = None) -> int:
    from cairn.embed import get_embedder

    path = path or sock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _unlink_quiet(path)
    # in-process load lives only here — clients must not pass skip_socket=False
    embedder = get_embedder(spec, dims, skip_socket=True)
    last = [time.monotonic()]
    lock = threading.Lock()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(16)
    srv.settimeout(1.0)
    _accept_until_idle(srv, path, embedder, lock, last, idle)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cairn-embedd", description=__doc__)
    p.add_argument("--spec", default="fastembed", help="Embedder spec (default fastembed).")
    p.add_argument("--sock", default=None, help="Unix socket path.")
    p.add_argument("--idle", type=int, default=DEFAULT_IDLE,
                   help="Exit after this many idle seconds (0 = never). Default 900.")
    p.add_argument("--dims", type=int, default=None, help="Cached dims (skip probe).")
    args = p.parse_args(argv)
    path = Path(args.sock) if args.sock else sock_path()
    try:
        return serve_forever(args.spec, path, idle=args.idle, dims=args.dims)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
