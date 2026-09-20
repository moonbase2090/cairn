"""Memory galaxy — VectorVault-style interactive starfield.

Stars are PCA of embeddings (x/y/z from the first three components), colored
by agent, clustered by task. The page is the VectorVault canvas (glow, tour,
search, inspector) rebranded for Cairn, plus a WebGL 3D warp mode (orbit,
dive, hyperspace tour) in the same dependency-free file.
`cairn galaxy` hosts it on a local stdlib HTTP server (not `cairn serve`).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import numpy as np


def pca2(matrix: np.ndarray) -> np.ndarray:
    """Project (n, d) to (n, 2) via SVD. Deterministic."""
    x = np.asarray(matrix, dtype=np.float64)
    x = x - x.mean(axis=0, keepdims=True)
    _, _, vt = np.linalg.svd(x, full_matrices=False)
    return x @ vt[:2].T


def pca3(matrix: np.ndarray) -> np.ndarray:
    """Project (n, d) to (n, 3) via SVD. Deterministic. Powers the 3D warp mode."""
    x = np.asarray(matrix, dtype=np.float64)
    x = x - x.mean(axis=0, keepdims=True)
    _, _, vt = np.linalg.svd(x, full_matrices=False)
    return x @ vt[:3].T


def _norm_axis(vals: np.ndarray) -> np.ndarray:
    lo, hi = float(vals.min()), float(vals.max())
    span = (hi - lo) or 1.0
    return (vals - lo) / span * 2 - 1


def _isle_layout(teams: list) -> dict:
    """Ring packing: team -> (cx, cy, radius) in [-1, 1]^2.

    Each team gets its own island so multi-team vaults read as an archipelago
    instead of one pile-up. Single team -> identity (0, 0, 1). Deterministic
    by sorted team id. Ring math budgets (center + radius) to exactly fill the
    unit square with a small margin, for any team count.
    """
    import math

    uniq = sorted(set(teams), key=lambda t: (t is None, t))
    n = len(uniq)
    if n <= 1:
        return {uniq[0]: (0.0, 0.0, 1.0)} if uniq else {}
    r = 1.0 / (1.0 + 1.0 / math.sin(math.pi / n)) * 0.92
    rc = 1.0 - r
    return {
        t: (rc * math.cos(2 * math.pi * i / n - math.pi / 2),
            rc * math.sin(2 * math.pi * i / n - math.pi / 2), r)
        for i, t in enumerate(uniq)
    }


def _template() -> str:
    try:
        from importlib.resources import files

        return files("cairn").joinpath("templates/galaxy-2d.html").read_text()
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return Path(__file__).with_name("templates").joinpath("galaxy-2d.html").read_text()


def to_points(client, limit: int = 2000) -> list[dict]:
    """VV-shaped point records with x,y,z in [-1, 1]. Includes archived/superseded (dim)."""
    rows = client.vault.scan("", (), limit)
    vec_by_row = dict(client.vault.all_vectors())
    mat, meta = [], []
    for r in rows:
        blob = vec_by_row.get(r["rowid"])
        if blob is None:
            continue
        mat.append(np.frombuffer(blob, dtype=np.float32).astype(np.float64))
        content = r["content"] or r["content_summary"] or ""
        meta.append(r)
    if not mat:
        return []
    if len(mat) > 3:
        coords = pca3(np.stack(mat))
        coords[:, 0] = _norm_axis(coords[:, 0])
        coords[:, 1] = _norm_axis(coords[:, 1])
        coords[:, 2] = _norm_axis(coords[:, 2])
    elif len(mat) > 2:
        coords = np.zeros((len(mat), 3))
        coords[:, :2] = pca2(np.stack(mat))
        coords[:, 0] = _norm_axis(coords[:, 0])
        coords[:, 1] = _norm_axis(coords[:, 1])
    else:
        coords = np.zeros((len(mat), 3))
    points = []
    isles = _isle_layout([r["team_id"] for r in meta])
    multi = len(isles) > 1
    from .store import ContentIntegrityError

    for r, (x, y, z) in zip(meta, coords):
        try:
            content = client.vault.read_content(r)
        except ContentIntegrityError:
            continue  # corrupt doc: omit the star, keep the galaxy up
        content = content or r["content_summary"] or ""
        if multi:  # shrink the team's PCA cloud onto its own island
            cx, cy, rad = isles[r["team_id"]]
            x, y, z = cx + x * rad, cy + y * rad, z * rad
        p = {
            "key": r["key"],
            "x": round(float(x), 4),
            "y": round(float(y), 4),
            "z": round(float(z), 4),
            "agent": r["agent_id"],
            "team": r["team_id"],
            "task": r["task_id"],
            "type": r["memory_type"],
            "status": r["status"],
            "version": int(r["version"] or 1),
            "created": int(r["created_at"] or 0),
            "stored_by": r["agent_id"],
            "text": content[:280],
        }
        if len(content) > 280:
            p["full"] = content
        points.append(p)
    return points


def render_html(points: list[dict]) -> str:
    data = json.dumps(points, ensure_ascii=False).replace("</", "<\\/")
    return _template().replace("__DATA__", data)


def galaxy_html(client, limit: int = 2000) -> tuple[str, int]:
    """Build the starfield HTML from the current vault. Returns (html, n)."""
    points = to_points(client, limit)
    return render_html(points), len(points)


def galaxy(client, out_path: str | Path | None = None, limit: int = 2000) -> dict:
    html_body, n = galaxy_html(client, limit)
    result: dict = {"memories": n}
    if out_path is not None:
        out = Path(out_path)
        out.write_text(html_body)
        result["out"] = str(out)
    return result


def _factory_for(client):
    from cairn.client import CairnClient
    from cairn.store import Vault

    vault, embedder, agent = client.vault, client.embedder, client.agent_id

    def make():
        v = Vault(vault.db_path, embedder.name, embedder.dims)
        return CairnClient(v, agent, embedder)

    return make


class _Handler(BaseHTTPRequestHandler):
    server_version = "cairn-galaxy/1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/galaxy.html"):
            client = self.server.client_factory()
            try:
                body, n = galaxy_html(client, self.server.limit)
            finally:
                client.vault.close()
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/health":
            raw = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/search":
            # BM25 keyword search for the header box: rank-ordered keys.
            # Same semantics as `list --search` (active-only, expired dropped).
            import time
            from urllib.parse import parse_qs

            qs = parse_qs(urlparse(self.path).query)
            q = qs.get("q", [""])[0]
            try:
                lim = min(int(qs.get("limit", [str(self.server.limit)])[0]), 5000)
            except ValueError:
                lim = self.server.limit
            client = self.server.client_factory()
            try:
                try:
                    now = time.time()
                    rows = client.vault.fts_search(q, lim) if q.strip() else []
                    keys = [r["key"] for r in rows
                            if r["expires_at"] is None or r["expires_at"] > now]
                    raw = json.dumps({"q": q, "keys": keys}).encode()
                    self.send_response(200)
                except RuntimeError as e:  # sqlite without FTS5
                    raw = json.dumps({"q": q, "error": str(e)}).encode()
                    self.send_response(503)
            finally:
                client.vault.close()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/memory":
            # full record + version history + audit trail for the expand view
            from urllib.parse import parse_qs

            qs = parse_qs(urlparse(self.path).query)
            key = qs.get("key", [""])[0]
            client = self.server.client_factory()
            try:
                rec = client.get_memory(key)
                if rec is None:
                    raw = json.dumps({"error": f"not found: {key}"}).encode()
                    self.send_response(404)
                else:
                    rows = client.vault.scan("canonical_id=?", (rec.canonical_id,), 50)
                    versions = []
                    for r in rows:
                        try:
                            vcontent = client.vault.read_content(r)
                        except ContentIntegrityError:
                            vcontent = None  # flagged in the expand view, not fatal
                        versions.append({
                            "key": r["key"], "version": int(r["version"] or 1),
                            "status": r["status"], "agent_id": r["agent_id"],
                            "created_at": int(r["created_at"] or 0),
                            "content": vcontent,
                        })
                    audit = _audit_for(client.vault.db_path.parent, key, rec.canonical_id)
                    raw = json.dumps({"record": rec.to_dict(), "versions": versions,
                                      "audit": audit}).encode()
                    self.send_response(200)
            finally:
                client.vault.close()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_error(404)


def _audit_for(vault_dir, key: str, canonical_id: str, limit: int = 200) -> list[dict]:
    """Audit trail lines touching this memory (by key or canonical id)."""
    from pathlib import Path

    out = []
    try:
        with open(Path(vault_dir) / "audit.jsonl", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("key") == key or e.get("canonical_id") == canonical_id \
                        or e.get("supersedes") == key:
                    out.append(e)
    except OSError:
        return []
    return out[-limit:]


def bind(client, host: str = "127.0.0.1", port: int = 8780, limit: int = 2000):
    """Bind a galaxy HTTP server. Caller serve_forever() or start a thread."""
    srv = ThreadingHTTPServer((host, port), _Handler)
    srv.client_factory = _factory_for(client)
    srv.limit = limit
    return srv


def start_background(client, host: str = "127.0.0.1", port: int = 0, limit: int = 2000):
    """Serve the galaxy in a daemon thread. Returns the server (has .server_address)."""
    srv = bind(client, host, port, limit)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def serve_forever(client, host: str = "127.0.0.1", port: int = 8780, limit: int = 2000) -> None:
    srv = bind(client, host, port, limit)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def galaxy_url(host: str, port: int) -> str:
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{shown}:{port}/"


def galaxy_alive(host: str, port: int, timeout: float = 1.0) -> bool:
    """True if a cairn galaxy answers /health on host:port."""
    import json
    from urllib.request import urlopen

    try:
        with urlopen(galaxy_url(host, port) + "health", timeout=timeout) as resp:
            return resp.status == 200 and json.loads(resp.read().decode()).get("ok") is True
    except Exception:
        return False
