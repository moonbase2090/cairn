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
import os
import sys
from pathlib import Path

from cairn import __version__ as CAIRN_VERSION
from cairn.client import CairnClient
from cairn.embed import format_embedder_hint, get_embedder, parse_embedder_hint
from cairn.store import Vault
from cairn.tutorial import howto_text

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
]


def make_client() -> CairnClient:
    from cairn.cli import default_agent_id

    vdir = Path(os.environ["CAIRN_DIR"]) if os.environ.get("CAIRN_DIR") else Path.cwd() / ".cairn"
    db = vdir / "vault.db"
    if not db.exists():
        raise FileNotFoundError(f"no vault at {db} — run `cairn init` + `cairn bootstrap` first")
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
    return CairnClient(Vault(db, embedder.name, embedder.dims), agent, embedder,
                       audit_path=vdir / "audit.jsonl")


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


def _tool_retrieve(client: CairnClient, args: dict) -> str:
    filters = {k: args[k] for k in ("task_id", "team_id", "memory_type") if args.get(k)}
    recs = client.retrieve_memory(
        args["query"], filters or None, int(args.get("top_k", 5)), args.get("min_sim"),
    )
    return _dump([m.to_dict() for m in recs])


def _tool_store(client: CairnClient, args: dict) -> str:
    res = client.store_memory(
        args["content"], args["team_id"], args["task_id"],
        args.get("memory_type", "semantic"), args.get("origin", "agent"),
        args.get("supersedes_key"), args.get("mode", "auto"),
    )
    return _dump(res.to_dict())


def _tool_list(client: CairnClient, args: dict) -> str:
    filters = {
        k: args[k] for k in ("task_id", "canonical_id", "memory_type", "status", "search")
        if args.get(k)
    }
    if not filters:
        raise ValueError("list_memories needs task_id, canonical_id, or search")
    recs = client.list_memories(filters, int(args.get("limit", 100)))
    return _dump([m.to_dict() for m in recs])


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
    return json.dumps({
        "agent": client.agent_id, "embedder": client.embedder.name,
        "dims": client.embedder.dims, "memories": client.vault.count(),
    }, default=str)


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


_TOOLS = {
    "retrieve_memory": _tool_retrieve,
    "store_memory": _tool_store,
    "list_memories": _tool_list,
    "get_memory": _tool_get,
    "archive_memory": _tool_archive,
    "restore_memory": _tool_restore,
    "cairn_howto": _tool_howto,
    "cairn_whoami": _tool_whoami,
    "cairn_gc": _tool_gc,
    "cairn_export": _tool_export,
    "cairn_import": _tool_import,
    "cairn_ingest": _tool_ingest,
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
