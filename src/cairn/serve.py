"""Team sync over HTTP or HTTPS — stdlib only. One vault serves, others push/pull packs.

Server holds its own vault; `push` imports a pack into it, `pull` exports from
it. V1 snapshots stay insert-only; v2 event sync applies newer state, tombstones,
and conflict resolutions while preserving resumable cursors.
Bearer-token auth. Plain HTTP is allowed only on a loopback address.
A host other than loopback requires TLS 1.2+ and a bearer token.

Threading: every request reopens the storage backend (SQLite connections
never cross threads); the embedder is stateless and shared.
"""
from __future__ import annotations

import hashlib
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


def _factory_for(client: CairnClient):
    vault, embedder, agent = client.vault, client.embedder, client.agent_id

    def make():
        audit = vault.vault_dir / "audit.jsonl"
        return CairnClient(vault.reopen(), agent, embedder, audit_path=audit)

    return make


@contextmanager
def _request_client(server):
    client = server.client_factory()
    try:
        yield client
    finally:
        client.vault.close()


def _server_sync_cursor(vault, peer: str, direction: str, token_id: str) -> int:
    cursor = vault.get_sync_cursor(peer, direction, token_id)
    if cursor == 0 and token_id == "shared":
        # Older shared-token servers persisted the empty token ID.
        return vault.get_sync_cursor(peer, direction, "")
    return cursor


class _Handler(BaseHTTPRequestHandler):
    server_version = "cairn-sync/1"

    def log_message(self, *args):  # quiet — use the audit log, not stdout
        pass

    def _authorize(self, client) -> tuple[bool, str | None, bool, str | None]:
        """Return authorization, agent identity, curator role, and token ID."""
        token_mode = getattr(self.server, "token_mode", "shared")
        authorization = self.headers.get("Authorization", "")
        if token_mode == "per-agent":
            if not authorization.startswith("Bearer "):
                return False, None, False, None
            bearer = authorization.removeprefix("Bearer ")
            if not bearer or bearer.strip() != bearer:
                return False, None, False, None
            digest = hashlib.sha256(bearer.encode("utf-8")).hexdigest()
            token = client.vault.get_server_token(digest)
            return (token is not None, token["agent_id"] if token is not None else None,
                    bool(token["curator"]) if token is not None else False,
                    token["token_id"] if token is not None else None)

        token = getattr(self.server, "token", "")
        if not token:
            return True, None, False, "shared"
        return hmac.compare_digest(authorization, f"Bearer {token}"), None, False, "shared"

    @staticmethod
    def _sync_pack_matches_agent(pack: dict, agent_id: str, curator: bool, client) -> bool:
        events = pack.get("events") if isinstance(pack, dict) else None
        if not isinstance(events, list):
            return False
        for event in events:
            if not isinstance(event, dict):
                return False
            if client.vault.has_sync_event(str(event.get("event_id", ""))):
                continue  # a previously accepted event may be relayed unchanged
            kind, key = event.get("kind"), event.get("key")
            resolution = event.get("conflict_resolution")
            if (resolution is not None and not curator
                    and (not isinstance(resolution, dict)
                         or resolution.get("resolved_by") != agent_id)):
                return False
            existing = client.vault.get(key) if isinstance(key, str) else None
            if kind == "snapshot":
                snapshot = event.get("snapshot")
                if not isinstance(snapshot, dict):
                    return False
                owner = snapshot.get("agent_id")
                if not isinstance(owner, str) or not isinstance(key, str) or not key.startswith(f"mem_{owner}_"):
                    return False
                if existing is None:
                    if owner != agent_id:
                        return False
                    parent = snapshot.get("supersedes")
                    parent_row = client.vault.get(parent) if isinstance(parent, str) else None
                    if parent_row and parent_row["agent_id"] != agent_id and not curator:
                        return False
                elif existing["agent_id"] != agent_id and not curator:
                    # Pulled foreign rows may be echoed unchanged, but an agent
                    # cannot advance another owner's state through a snapshot.
                    if (existing["content_hash"] != snapshot.get("content_hash")
                            or existing["status"] != snapshot.get("status")
                            or existing["archived_at"] != snapshot.get("archived_at")
                            or existing["expires_at"] != snapshot.get("expires_at")):
                        return False
            elif kind in {"state", "tombstone"}:
                if existing is not None:
                    owner = existing["agent_id"]
                else:
                    owner = event.get("agent_id")
                if owner != agent_id and not curator:
                    return False
            else:
                return False
        return True

    @staticmethod
    def _pack_matches_agent(pack: dict, agent_id: str, client, curator: bool = False) -> bool:
        memories = pack.get("memories") if isinstance(pack, dict) else None
        if not isinstance(memories, list):
            return True  # import_pack reports malformed pack structure
        existing_agents = client.vault.get_agent_ids([
            memory["key"] for memory in memories
            if isinstance(memory, dict) and isinstance(memory.get("key"), str)
        ])
        for memory in memories:
            if not isinstance(memory, dict):
                return False
            memory_agent = memory.get("agent_id")
            key = memory.get("key")
            if not isinstance(key, str) or not key.startswith(f"mem_{memory_agent}_"):
                return False
            if memory_agent == agent_id:
                if existing_agents.get(key) is None and not curator:
                    parent = memory.get("supersedes")
                    parent_row = client.vault.get(parent) if isinstance(parent, str) else None
                    if parent_row and parent_row["agent_id"] != agent_id:
                        return False
                continue
            # Clients pull the shared vault into their local vault. Permit those
            # unchanged rows to round-trip, but never let a token create another
            # agent's identity on the server.
            if existing_agents.get(key) != memory_agent:
                return False
        return True

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
                authorized, _agent_id, _curator, token_id = self._authorize(client)
                identity = client.vault.vault_identity
                result = {"ok": True, "memories": client.vault.count()}
                if authorized:
                    result.update({
                        "vault_id": identity.vault_id,
                        "vault_name": identity.name,
                        "origin_id": client.vault.sync_origin_id(),
                        "token_id": token_id,
                    })
                self._send(200, result)
            return
        if u.path == "/pull":
            qs = parse_qs(u.query)
            since = int(qs["since"][0]) if "since" in qs else None
            after = int(qs["after"][0]) if "after" in qs else None
            with _request_client(self.server) as client:
                authorized, _agent_id, _curator, token_id = self._authorize(client)
                if not authorized:
                    self._send(401, {"error": "unauthorized"})
                    return
                if after is None:
                    self._send(200, client.export(since))
                    return
                if after < 0:
                    self._send(400, {"error": "cursor cannot be negative"})
                    return
                peer_identity = self.headers.get("X-Cairn-Peer") or "anonymous"
                if after > _server_sync_cursor(client.vault, peer_identity, "pull", token_id):
                    self._send(409, {"error": "requested cursor is ahead of the saved peer cursor"})
                    return
                pack = client.export_delta(after)
                with client.vault.transaction():
                    client.vault.set_sync_cursor(
                        peer_identity, "pull", pack["cursor"], int(pack["exported_at"]),
                        token_id,
                    )
                self._send(200, pack)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in {"/push", "/conflicts/resolve"}:
            self._send(404, {"error": "not found"})
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
        if not isinstance(pack, dict):
            self._send(400, {"error": "request body must be an object"})
            return
        with _request_client(self.server) as client:
            authorized, agent_id, curator, token_id = self._authorize(client)
            if not authorized:
                self._send(401, {"error": "unauthorized"})
                return
            if self.path == "/conflicts/resolve":
                try:
                    result = client.resolve_competing_correction(
                        str(pack.get("base_key", "")), str(pack.get("winner_key", "")),
                        curator=curator,
                        resolver_agent=agent_id or str(pack.get("agent_id") or client.agent_id),
                    )
                except PermissionError as e:
                    self._send(403, {"error": str(e)})
                    return
                except (KeyError, ValueError) as e:
                    self._send(400, {"error": str(e)})
                    return
                self._send(200, result)
                return
            try:
                with client.vault.transaction():
                    if agent_id is not None:
                        if pack.get("pack") == "cairn-sync-2":
                            allowed = self._sync_pack_matches_agent(pack, agent_id, curator, client)
                        else:
                            allowed = self._pack_matches_agent(pack, agent_id, client, curator)
                        if not allowed:
                            self._send(403, {"error": "token is restricted to its assigned agent; curator permission is required for cross-agent state changes"})
                            return
                    if pack.get("pack") == "cairn-sync-2":
                        after, cursor = pack.get("after"), pack.get("cursor")
                        if (not isinstance(after, int) or isinstance(after, bool)
                                or not isinstance(cursor, int) or isinstance(cursor, bool)
                                or after < 0 or cursor < after):
                            self._send(400, {"error": "invalid sync pack cursor"})
                            return
                        peer_identity = pack.get("origin_id", "anonymous")
                        if after > _server_sync_cursor(
                            client.vault, peer_identity, "push", token_id,
                        ):
                            self._send(409, {"error": "pushed cursor skips events"})
                            return
                    out = client.import_pack(pack)
                    peer_identity = pack.get("origin_id", "anonymous")
                    if pack.get("pack") == "cairn-sync-2":
                        client.vault.set_sync_cursor(
                            peer_identity, "push", int(pack["cursor"]),
                            int(pack.get("exported_at", 0)), token_id,
                        )
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


def _bind(client, host: str, port: int, token: str, tls_cert: str | None,
          tls_key: str | None, token_mode: str = "shared"):
    if token_mode not in {"shared", "per-agent"}:
        raise ValueError("token mode must be 'shared' or 'per-agent'")
    if bool(tls_cert) != bool(tls_key):
        raise ValueError("HTTPS needs both --tls-cert and --tls-key")
    if not _is_loopback(host):
        if not tls_cert:
            raise ValueError("plain HTTP is only allowed on localhost; pass --tls-cert and --tls-key")
        if token_mode == "shared" and not token:
            raise ValueError("a bearer token is required when serving beyond localhost")
    srv = ThreadingHTTPServer((host, port), _Handler)
    srv.client_factory = _factory_for(client)
    srv.token = token
    srv.token_mode = token_mode
    if tls_cert:
        srv.socket = _tls_context(tls_cert, tls_key).wrap_socket(srv.socket, server_side=True)
    return srv


def start_background(client, host: str = "127.0.0.1", port: int = 0, token: str = "",
                     tls_cert: str | None = None, tls_key: str | None = None,
                     token_mode: str = "shared"):
    """Start the sync server in a daemon thread. Returns the server (has .server_address)."""
    srv = _bind(client, host, port, token, tls_cert, tls_key, token_mode)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def serve_forever(client, host: str = "127.0.0.1", port: int = 8778, token: str = "",
                  tls_cert: str | None = None, tls_key: str | None = None,
                  token_mode: str = "shared") -> None:
    srv = _bind(client, host, port, token, tls_cert, tls_key, token_mode)
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
              cafile: str | None = None, after: int | None = None,
              peer: str | None = None) -> dict:
    cursor_name, cursor_value = ("after", after) if after is not None else ("since", since)
    query = f"?{cursor_name}={cursor_value}" if cursor_value is not None else ""
    url = base_url.rstrip("/") + "/pull" + query
    headers = {"Authorization": f"Bearer {token}"}
    if peer:
        headers["X-Cairn-Peer"] = peer
    req = urlrequest.Request(url, headers=headers)
    with urlrequest.urlopen(req, timeout=30, context=_client_context(base_url, cafile)) as resp:
        return json.load(resp)


def health_from(base_url: str, cafile: str | None = None, token: str = "") -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urlrequest.Request(base_url.rstrip("/") + "/health", headers=headers)
    with urlrequest.urlopen(req, timeout=10, context=_client_context(base_url, cafile)) as resp:
        return json.load(resp)


def sync_handshake(base_url: str, vault_id: str, token: str = "",
                   cafile: str | None = None) -> dict:
    """Validate a peer's vault and token identity before starting a sync exchange."""
    peer = health_from(base_url, cafile=cafile, token=token)
    if peer.get("ok") is not True:
        raise ValueError("sync peer health check failed")
    peer_vault_id = peer.get("vault_id")
    if not isinstance(peer_vault_id, str) or not peer_vault_id:
        raise ValueError("sync peer is missing its vault identity")
    if peer_vault_id != vault_id:
        raise ValueError("sync peer belongs to a different vault")
    token_id = peer.get("token_id")
    if not isinstance(token_id, str) or not token_id:
        raise ValueError("sync peer did not identify the active token")
    origin_id = peer.get("origin_id")
    if not isinstance(origin_id, str) or not origin_id:
        raise ValueError("sync peer did not identify its vault replica")
    return peer


def push_to(base_url: str, pack: dict, token: str = "", cafile: str | None = None) -> dict:
    data = json.dumps(pack, default=str).encode()
    req = urlrequest.Request(
        base_url.rstrip("/") + "/push",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urlrequest.urlopen(req, timeout=60, context=_client_context(base_url, cafile)) as resp:
        return json.load(resp)


def resolve_conflict_to(base_url: str, base_key: str, winner_key: str,
                        token: str = "", agent_id: str = "",
                        cafile: str | None = None) -> dict:
    data = json.dumps({"base_key": base_key, "winner_key": winner_key,
                       "agent_id": agent_id}).encode()
    req = urlrequest.Request(
        base_url.rstrip("/") + "/conflicts/resolve", data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urlrequest.urlopen(req, timeout=30, context=_client_context(base_url, cafile)) as resp:
        return json.load(resp)
