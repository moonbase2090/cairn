"""Team sync over HTTP or HTTPS — stdlib only. One vault serves, others push/pull packs.

Server holds its own vault; `push` imports a pack into it, `pull` exports from
it. Merge semantics are identical to file packs (idempotent key-union).
Bearer-token auth. Plain HTTP is allowed only on a loopback address.
A host other than loopback requires TLS 1.2+ and a bearer token.

Threading: every request opens its own Vault connection (SQLite connections
never cross threads); the embedder is stateless and shared.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request as urlrequest
from urllib.parse import urlparse

from .client import CairnClient
from .store import Vault


def _factory_for(client: CairnClient):
    vault, embedder, agent = client.vault, client.embedder, client.agent_id

    def make():
        v = Vault(vault.db_path, embedder.name, embedder.dims)
        audit = vault.db_path.parent / "audit.jsonl"
        return CairnClient(v, agent, embedder, audit_path=audit)

    return make


@contextmanager
def _request_client(server):
    client = server.client_factory()
    try:
        yield client
    finally:
        client.vault.close()


class _Handler(BaseHTTPRequestHandler):
    server_version = "cairn-sync/1"

    def log_message(self, *args):  # quiet — use the audit log, not stdout
        pass

    def _authed(self) -> bool:
        token = getattr(self.server, "token", "")
        if not token:
            return True
        return hmac.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {token}"
        )

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse

        u = urlparse(self.path)
        if u.path == "/health":
            with _request_client(self.server) as client:
                self._send(200, {"ok": True, "memories": client.vault.count()})
            return
        if u.path == "/pull":
            if not self._authed():
                self._send(401, {"error": "unauthorized"})
                return
            qs = parse_qs(u.query)
            since = int(qs["since"][0]) if "since" in qs else None
            with _request_client(self.server) as client:
                self._send(200, client.export(since))
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/push":
            self._send(404, {"error": "not found"})
            return
        if not self._authed():
            self._send(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        try:
            pack = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            self._send(400, {"error": "invalid JSON pack"})
            return
        with _request_client(self.server) as client:
            try:
                out = client.import_pack(pack)
            except ValueError as e:  # cross-space pack
                self._send(400, {"error": str(e)})
                return
        self._send(200, out)


def _is_loopback(host: str) -> bool:
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _tls_context(cert: str | Path, key: str | Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return ctx


def _bind(client, host: str, port: int, token: str, tls_cert: str | None, tls_key: str | None):
    if bool(tls_cert) != bool(tls_key):
        raise ValueError("HTTPS needs both --tls-cert and --tls-key")
    if not _is_loopback(host):
        if not tls_cert:
            raise ValueError("plain HTTP is only allowed on localhost; pass --tls-cert and --tls-key")
        if not token:
            raise ValueError("a bearer token is required when serving beyond localhost")
    srv = ThreadingHTTPServer((host, port), _Handler)
    srv.client_factory = _factory_for(client)
    srv.token = token
    if tls_cert:
        srv.socket = _tls_context(tls_cert, tls_key).wrap_socket(srv.socket, server_side=True)
    return srv


def start_background(client, host: str = "127.0.0.1", port: int = 0, token: str = "",
                     tls_cert: str | None = None, tls_key: str | None = None):
    """Start the sync server in a daemon thread. Returns the server (has .server_address)."""
    srv = _bind(client, host, port, token, tls_cert, tls_key)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def serve_forever(client, host: str = "127.0.0.1", port: int = 8778, token: str = "",
                  tls_cert: str | None = None, tls_key: str | None = None) -> None:
    srv = _bind(client, host, port, token, tls_cert, tls_key)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def _client_context(base_url: str, cafile: str | None):
    parsed = urlparse(base_url)
    host = parsed.hostname or ""
    if parsed.scheme == "https":
        return ssl.create_default_context(cafile=cafile)
    if parsed.scheme == "http" and not _is_loopback(host):
        raise ValueError("refusing plain HTTP to a non-local host; use https://")
    return None


def pull_from(base_url: str, token: str = "", since: int | None = None,
              cafile: str | None = None) -> dict:
    url = base_url.rstrip("/") + "/pull" + (f"?since={since}" if since is not None else "")
    req = urlrequest.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urlrequest.urlopen(req, timeout=30, context=_client_context(base_url, cafile)) as resp:
        return json.load(resp)


def push_to(base_url: str, pack: dict, token: str = "", cafile: str | None = None) -> dict:
    data = json.dumps(pack, default=str).encode()
    req = urlrequest.Request(
        base_url.rstrip("/") + "/push",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urlrequest.urlopen(req, timeout=60, context=_client_context(base_url, cafile)) as resp:
        return json.load(resp)
