"""cairn-mcp — shared memory as MCP native tools (stdio, stdlib only).

Speaks newline-delimited JSON-RPC 2.0: initialize, tools/list, tools/call.
Configure via env (same resolution as the CLI): $CAIRN_DIR (or ./.cairn),
$CAIRN_AGENT, embedder from the vault's hint file.

Tools: the six memory verbs, cairn_howto (onboarding), plus ops parity —
cairn_whoami, cairn_gc, cairn_export, cairn_import, cairn_ingest.
`cairn serve` stays CLI-only (long-running process, not a tool call).
"""
from __future__ import annotations

import json
import hashlib
import os
import signal
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError

from cairn import __version__ as CAIRN_VERSION
from cairn.client import CairnClient
from cairn.embed import format_embedder_hint, get_embedder, parse_embedder_hint
from cairn.storage import open_backend
from cairn.tutorial import howto_text
from cairn.models import now_epoch
from cairn.serve import health_from, pull_from, push_to, resolve_conflict_to, sync_handshake

SERVER_NAME = "cairn"
SERVER_VERSION = CAIRN_VERSION
PROTOCOL_VERSION = "2024-11-05"

TOOL_DEFS = [
    {"name": "retrieve_memory", "description": "Semantic search over shared team memory. Returns latest version of each fact — cite keys (per mem_...).",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "task_id": {"type": "string"},
         "team_id": {"type": "string"}, "memory_type": {"type": "string"},
         "top_k": {"type": "integer", "default": 5},
         "min_sim": {"type": "number", "description": "Drop hits below this cosine similarity."}}, "required": ["query"]}},
    {"name": "store_memory", "description": "Store a fact/decision/summary. Exact dups are no-ops; near-dups return duplicate_detected with candidates.",
     "inputSchema": {"type": "object", "properties": {
         "content": {"type": "string"}, "team_id": {"type": "string"}, "task_id": {"type": "string"},
         "memory_type": {"type": "string", "default": "semantic"},
         "origin": {"type": "string", "default": "agent"},
         "supersedes_key": {"type": "string"}, "mode": {"type": "string", "default": "auto"}},
         "required": ["content", "team_id", "task_id"]}},
    {"name": "list_memories", "description": "Exact lookups by task/canonical id, or BM25 keyword search via search (literal terms, best match first).",
     "inputSchema": {"type": "object", "properties": {
         "task_id": {"type": "string"}, "canonical_id": {"type": "string"},
         "memory_type": {"type": "string"}, "status": {"type": "string"},
         "search": {"type": "string", "description": "BM25 keyword search over content."},
         "limit": {"type": "integer", "default": 100}}}},
    {"name": "get_memory", "description": "Fetch one memory by exact key.",
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "archive_memory", "description": "Retract a wrong memory (stops surfacing; grace-deleted later).",
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "restore_memory", "description": "Undo a bad correction or archive.",
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "cairn_howto", "description": "Onboarding: how to use shared memory (retrieve-first, cite keys, trust model). Call this first in a new project.",
     "inputSchema": {"type": "object", "properties": {"topic": {"type": "string"}}}},
    {"name": "cairn_whoami", "description": "This seat's identity, embedder, and vault size.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "cairn_secret_scan",
     "description": "Read-only preflight scan of existing vault content; returns category counts without content or memory identifiers.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "cairn_gc", "description": "Lifecycle sweep. Dry-run unless apply=true (promote stale superseded, delete archived/expired, circuit-breaker capped).",
     "inputSchema": {"type": "object", "properties": {"apply": {"type": "boolean", "default": False}}}},
    {"name": "cairn_export", "description": "Export a sync pack (merge it elsewhere with cairn_import).",
     "inputSchema": {"type": "object", "properties": {"since": {"type": "integer"}}}},
    {"name": "cairn_import", "description": "Merge a sync pack (idempotent key-union; refuses cross-space packs).",
     "inputSchema": {"type": "object", "properties": {"pack": {"type": "object"}}, "required": ["pack"]}},
    {"name": "cairn_ingest", "description": "Seed the vault from a docs directory on this machine (chunked per section, idempotent).",
     "inputSchema": {"type": "object", "properties": {
         "dir": {"type": "string"}, "team": {"type": "string"},
         "memory_type": {"type": "string", "default": "document"}},
         "required": ["dir", "team"]}},
    {"name": "cairn_sync", "description": "Push or pull resumable sync deltas, inspect peer cursors and conflicts, or check server health.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["push", "pull", "status", "health"]},
         "url": {"type": "string"}, "token": {"type": "string"},
         "tls_ca": {"type": "string"}}, "required": ["action"]}},
    {"name": "cairn_serve", "description": "Start, inspect, health-check, or stop this vault's sync server.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["start", "status", "health", "stop"]},
         "host": {"type": "string", "default": "127.0.0.1"},
         "port": {"type": "integer", "default": 8778},
         "token_mode": {"type": "string", "enum": ["shared", "per-agent"], "default": "per-agent"},
         "token": {"type": "string"}, "tls_cert": {"type": "string"},
         "tls_key": {"type": "string"}, "url": {"type": "string"},
         "tls_ca": {"type": "string"}}, "required": ["action"]}},
    {"name": "cairn_token", "description": "Create, list, or revoke per-agent server tokens. A created token is shown once.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["create", "list", "revoke"]},
         "agent": {"type": "string"}, "curator": {"type": "boolean", "default": False},
         "token_id": {"type": "string"}}, "required": ["action"]}},
    {"name": "cairn_conflicts", "description": "List competing corrections or resolve one by selecting its active winner.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["list", "resolve"]},
         "base_key": {"type": "string"}, "winner_key": {"type": "string"},
         "url": {"type": "string"}, "token": {"type": "string"},
         "tls_ca": {"type": "string"}},
         "required": ["action"]}},
]

_SERVERS: dict[str, subprocess.Popen] = {}


def make_client() -> CairnClient:
    from cairn.cli import default_agent_id, storage_config

    vdir = Path(os.environ["CAIRN_DIR"]) if os.environ.get("CAIRN_DIR") else Path.cwd() / ".cairn"
    try:
        spec, dims = parse_embedder_hint((vdir / "embedder").read_text())
        spec = spec or "hash"
    except OSError:
        spec, dims = "hash", None
    agent = os.environ.get("CAIRN_AGENT") or default_agent_id()
    embedder = get_embedder(spec, dims)
    if dims is None:
        try:
            (vdir / "embedder").write_text(format_embedder_hint(spec, embedder.dims))
        except OSError:
            pass
    vault = open_backend(vdir, embedder.name, embedder.dims, config=storage_config(vdir))
    return CairnClient(vault, agent, embedder,
                       audit_path=vdir / "audit.jsonl")


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


def _with_vault_identity(client: CairnClient, value):
    identity = client.vault.vault_identity
    fields = {"vault_name": identity.name, "vault_id": identity.vault_id}
    if isinstance(value, dict):
        return {**value, **fields}
    if isinstance(value, list):
        return [{**item, **fields} for item in value]
    return value


def _tool_retrieve(client: CairnClient, args: dict) -> str:
    filters = {k: args[k] for k in ("task_id", "team_id", "memory_type") if args.get(k)}
    recs = client.retrieve_memory(
        args["query"], filters or None, int(args.get("top_k", 5)), args.get("min_sim"),
    )
    return _dump(_with_vault_identity(client, [m.to_dict() for m in recs]))


def _tool_store(client: CairnClient, args: dict) -> str:
    res = client.store_memory(
        args["content"], args["team_id"], args["task_id"],
        args.get("memory_type", "semantic"), args.get("origin", "agent"),
        args.get("supersedes_key"), args.get("mode", "auto"),
    )
    return _dump(_with_vault_identity(client, res.to_dict()))


def _tool_list(client: CairnClient, args: dict) -> str:
    filters = {
        k: args[k] for k in ("task_id", "canonical_id", "memory_type", "status", "search")
        if args.get(k)
    }
    if not filters:
        raise ValueError("list_memories needs task_id, canonical_id, or search")
    recs = client.list_memories(filters, int(args.get("limit", 100)))
    return _dump(_with_vault_identity(client, [m.to_dict() for m in recs]))


def _tool_get(client: CairnClient, args: dict) -> str:
    rec = client.get_memory(args["key"])
    return _dump(rec.to_dict() if rec else {"found": False})


def _tool_archive(client: CairnClient, args: dict) -> str:
    return json.dumps(client.archive_memory(args["key"]), default=str)


def _tool_restore(client: CairnClient, args: dict) -> str:
    return json.dumps(client.restore_memory(args["key"]), default=str)


def _tool_howto(_client: CairnClient, args: dict) -> str:
    return howto_text(args.get("topic"))


def _tool_whoami(client: CairnClient, _args: dict) -> str:
    identity = client.vault.vault_identity
    role = "curator" if any(
        token["agent_id"] == client.agent_id and bool(token["curator"])
        for token in client.vault.list_server_tokens()
    ) else "member"
    return json.dumps({
        "vault_name": identity.name, "vault_id": identity.vault_id,
        "agent": client.agent_id, "storage": client.vault.name, "role": role,
        "embedder": client.embedder.name, "dims": client.embedder.dims,
        "memories": client.vault.count(),
    }, default=str)


def _tool_secret_scan(client: CairnClient, _args: dict) -> str:
    return _dump(client.preflight_secret_scan())


def _tool_gc(client: CairnClient, args: dict) -> str:
    return json.dumps(client.gc(dry_run=not bool(args.get("apply", False))), default=str)


def _tool_export(client: CairnClient, args: dict) -> str:
    return json.dumps(client.export(args.get("since")), default=str)


def _tool_import(client: CairnClient, args: dict) -> str:
    if not isinstance(args.get("pack"), dict):
        raise TypeError("cairn_import needs a pack object from cairn_export")
    return json.dumps(client.import_pack(args["pack"]), default=str)


def _tool_ingest(client: CairnClient, args: dict) -> str:
    from cairn.ingest import ingest_dir

    return json.dumps(ingest_dir(
        client, args["team"], args["dir"], args.get("memory_type", "document"),
    ), default=str)


def _tool_sync(client: CairnClient, args: dict) -> str:
    action = args.get("action")
    url = args.get("url") or os.environ.get("CAIRN_URL")
    if action == "status":
        if not url:
            return _dump(client.sync_status())
        token = args.get("token") or os.environ.get("CAIRN_TOKEN", "")
        handshake = sync_handshake(
            url, client.vault.vault_identity.vault_id, token=token,
            cafile=args.get("tls_ca"),
        )
        return _dump(client.sync_status(handshake["origin_id"]))
    if action == "health":
        if not url:
            raise ValueError("cairn_sync health needs url or CAIRN_URL")
        return _dump(health_from(url, cafile=args.get("tls_ca")))
    if action not in {"push", "pull"}:
        raise ValueError("cairn_sync action must be push, pull, status, or health")
    if not url:
        raise ValueError("cairn_sync push/pull needs url or CAIRN_URL")
    token = args.get("token") or os.environ.get("CAIRN_TOKEN", "")
    handshake = sync_handshake(
        url, client.vault.vault_identity.vault_id, token=token,
        cafile=args.get("tls_ca"),
    )
    token_id = handshake["token_id"]
    peer = handshake["origin_id"]
    if action == "push":
        after = client.vault.get_sync_cursor(peer, "push", token_id)
        pack = client.export_delta(after)
        result = push_to(url, pack, token=token, cafile=args.get("tls_ca"))
        cursor = int(result.get("cursor", after))
        if cursor < after or cursor > pack["cursor"]:
            raise ValueError("server returned an invalid push cursor")
        with client.vault.transaction():
            client.vault.set_sync_cursor(peer, "push", cursor, now_epoch(), token_id)
        return _dump(result)
    after = client.vault.get_sync_cursor(peer, "pull", token_id)
    pack = pull_from(url, token=token, cafile=args.get("tls_ca"), after=after,
                     peer=client.vault.sync_origin_id())
    return _dump(client.import_sync_pack(
        pack, peer=peer, direction="pull", token_id=token_id,
    ))


def _serve_state_path(client: CairnClient) -> Path:
    return client.vault.vault_dir / "sync-server.pid"


def _tool_serve(client: CairnClient, args: dict) -> str:
    action = args.get("action")
    state_path = _serve_state_path(client)
    if action == "start":
        host = str(args.get("host", "127.0.0.1"))
        port = int(args.get("port", 8778))
        mode = args.get("token_mode", "per-agent")
        if mode not in {"shared", "per-agent"}:
            raise ValueError("token_mode must be shared or per-agent")
        existing = _read_serve_state(state_path)
        if existing and _process_running(existing["pid"]):
            return _dump({"running": True, "pid": existing["pid"], "url": existing["url"],
                          "already_running": True})
        tls_cert, tls_key = args.get("tls_cert"), args.get("tls_key")
        if bool(tls_cert) != bool(tls_key):
            raise ValueError("HTTPS needs both tls_cert and tls_key")
        token = args.get("token") or os.environ.get("CAIRN_TOKEN")
        child_env = dict(os.environ)
        if mode == "per-agent":
            child_env.pop("CAIRN_TOKEN", None)
        elif not token:
            raise ValueError("shared token mode needs token or CAIRN_TOKEN")
        else:
            child_env["CAIRN_TOKEN"] = token
        logfile = client.vault.vault_dir / "sync-server.log"
        command = [sys.executable, "-m", "cairn.cli", "--vault",
                   str(client.vault.vault_dir), "--agent-id", client.agent_id,
                   "serve", "--host", host, "--port", str(port), "--token-mode", mode]
        if tls_cert:
            command.extend(["--tls-cert", str(tls_cert), "--tls-key", str(tls_key)])
        with open(logfile, "ab") as log:
            proc = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=child_env, start_new_session=True,
            )
        _SERVERS[str(state_path)] = proc
        scheme = "https" if tls_cert else "http"
        url = f"{scheme}://{host}:{port}"
        deadline = time.monotonic() + 5
        while True:
            if proc.poll() is not None:
                _SERVERS.pop(str(state_path), None)
                raise RuntimeError(f"sync server exited during startup; inspect {logfile}")
            try:
                if health_from(url, cafile=args.get("tls_ca")).get("ok") is True:
                    break
            except (URLError, OSError, TimeoutError):
                pass
            if time.monotonic() >= deadline:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
                _SERVERS.pop(str(state_path), None)
                raise RuntimeError(f"sync server did not become healthy; inspect {logfile}")
            time.sleep(0.05)
        state_path.write_text(json.dumps({"pid": proc.pid, "url": url, "mode": mode}))
        return _dump({"running": True, "pid": proc.pid, "url": url, "token_mode": mode})
    if action == "status":
        state = _read_serve_state(state_path)
        proc = _SERVERS.get(str(state_path))
        running = bool(state and (proc.poll() is None if proc else _process_running(state["pid"])))
        return _dump({"running": running, **(state or {})})
    if action == "health":
        url = args.get("url")
        if not url:
            state = _read_serve_state(state_path)
            url = state["url"] if state else None
        if not url:
            raise ValueError("cairn_serve health needs url or a running server")
        return _dump(health_from(url, cafile=args.get("tls_ca")))
    if action == "stop":
        state = _read_serve_state(state_path)
        proc = _SERVERS.get(str(state_path))
        running = proc.poll() is None if proc else bool(state and _process_running(state["pid"]))
        if not state or not running:
            state_path.unlink(missing_ok=True)
            return _dump({"running": False, "stopped": False})
        pid = int(state["pid"])
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        else:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _process_running(pid):
                time.sleep(0.05)
            if _process_running(pid):
                raise RuntimeError(f"sync server process {pid} did not stop")
        _SERVERS.pop(str(state_path), None)
        state_path.unlink(missing_ok=True)
        return _dump({"running": False, "stopped": True})
    raise ValueError("cairn_serve action must be start, status, health, or stop")


def _read_serve_state(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) and isinstance(data.get("pid"), int) else None
    except (OSError, ValueError):
        return None


def _process_running(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _tool_token(client: CairnClient, args: dict) -> str:
    action = args.get("action")
    if action == "create":
        agent_id = str(args.get("agent", "")).strip()
        if not agent_id:
            raise ValueError("cairn_token create needs a non-empty agent")
        token_id = "ct_" + secrets.token_hex(6)
        token = "cairn_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        curator = bool(args.get("curator", False))
        with client.vault.transaction():
            client.vault.create_server_token(token_id, digest, agent_id, now_epoch(), curator)
        return _dump({"token_id": token_id, "agent_id": agent_id,
                      "curator": curator, "token": token})
    if action == "list":
        return _dump([dict(row) for row in client.vault.list_server_tokens()])
    if action == "revoke":
        token_id = str(args.get("token_id", ""))
        if not token_id:
            raise ValueError("cairn_token revoke needs token_id")
        with client.vault.transaction():
            revoked = client.vault.delete_server_token(token_id)
        if not revoked:
            raise ValueError(f"no active token with id {token_id!r}")
        return _dump({"token_id": token_id, "revoked": True})
    raise ValueError("cairn_token action must be create, list, or revoke")


def _tool_conflicts(client: CairnClient, args: dict) -> str:
    action = args.get("action")
    if action == "list":
        return _dump(client.sync_status()["conflicts"])
    if action == "resolve":
        if not args.get("base_key") or not args.get("winner_key"):
            raise ValueError("cairn_conflicts resolve needs base_key and winner_key")
        url = args.get("url") or os.environ.get("CAIRN_URL")
        token = args.get("token") or os.environ.get("CAIRN_TOKEN", "")
        if url:
            return _dump(resolve_conflict_to(
                url, args["base_key"], args["winner_key"], token=token,
                agent_id=client.agent_id, cafile=args.get("tls_ca"),
            ))
        return _dump(client.resolve_competing_correction(args["base_key"], args["winner_key"]))
    raise ValueError("cairn_conflicts action must be list or resolve")


_TOOLS = {
    "retrieve_memory": _tool_retrieve,
    "store_memory": _tool_store,
    "list_memories": _tool_list,
    "get_memory": _tool_get,
    "archive_memory": _tool_archive,
    "restore_memory": _tool_restore,
    "cairn_howto": _tool_howto,
    "cairn_whoami": _tool_whoami,
    "cairn_secret_scan": _tool_secret_scan,
    "cairn_gc": _tool_gc,
    "cairn_export": _tool_export,
    "cairn_import": _tool_import,
    "cairn_ingest": _tool_ingest,
    "cairn_sync": _tool_sync,
    "cairn_serve": _tool_serve,
    "cairn_token": _tool_token,
    "cairn_conflicts": _tool_conflicts,
}


def call_tool(client: CairnClient, name: str, args: dict) -> str:
    fn = _TOOLS.get(name)
    if fn is None:
        raise ValueError(f"unknown tool: {name}")
    return fn(client, args)


def respond(msg_id, result=None, error=None) -> None:
    msg: dict = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, default=str) + "\n")
    sys.stdout.flush()


def _rpc_initialize(_client, msg_id, _msg) -> None:
    respond(msg_id, {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    })


def _rpc_ping(_client, msg_id, _msg) -> None:
    respond(msg_id, {})


def _rpc_tools(_client, msg_id, _msg) -> None:
    respond(msg_id, {"tools": TOOL_DEFS})


def _rpc_call(client, msg_id, msg) -> None:
    params = msg.get("params", {})
    try:
        text = call_tool(client, params.get("name", ""), params.get("arguments", {}))
        respond(msg_id, {"content": [{"type": "text", "text": text}]})
    except (KeyError, ValueError, TypeError) as e:
        respond(msg_id, error={"code": -32602, "message": str(e)})
    except Exception as e:  # noqa: BLE001 — a tool bug must return JSON-RPC, not kill stdio
        sys.stderr.write(f"cairn-mcp tool error: {type(e).__name__}: {e}\n")
        respond(msg_id, error={"code": -32603, "message": f"{type(e).__name__}: {e}"})


def _rpc_noop(_client, _msg_id, _msg) -> None:
    return None


_RPC = {
    "initialize": _rpc_initialize,
    "notifications/initialized": _rpc_noop,
    "ping": _rpc_ping,
    "tools/list": _rpc_tools,
    "tools/call": _rpc_call,
}


def handle(client: CairnClient, msg: dict) -> bool:
    """Returns False to shut down."""
    method = msg.get("method")
    msg_id = msg.get("id")
    fn = _RPC.get(method)
    if fn is not None:
        fn(client, msg_id, msg)
    elif method is not None and msg_id is not None:
        respond(msg_id, error={"code": -32601, "message": f"unknown method: {method}"})
    return True


def _read_stdio(client: CairnClient) -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not handle(client, msg):
            break
    return 0


def main() -> int:
    try:
        client = make_client()
    except (FileNotFoundError, ValueError, ImportError) as e:
        sys.stderr.write(f"cairn-mcp: {e}\n")
        return 2
    return _read_stdio(client)


if __name__ == "__main__":
    sys.exit(main())
