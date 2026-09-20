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


def call_tool(client: CairnClient, name: str, args: dict) -> str:
    if name == "retrieve_memory":
        filters = {k: args[k] for k in ("task_id", "team_id", "memory_type") if args.get(k)}
        recs = client.retrieve_memory(args["query"], filters or None, int(args.get("top_k", 5)),
                                      args.get("min_sim"))
        return json.dumps([m.to_dict() for m in recs], indent=2, default=str)
    if name == "store_memory":
        res = client.store_memory(args["content"], args["team_id"], args["task_id"],
                                  args.get("memory_type", "semantic"), args.get("origin", "agent"),
                                  args.get("supersedes_key"), args.get("mode", "auto"))
        return json.dumps(res.to_dict(), indent=2, default=str)
    if name == "list_memories":
        filters = {k: args[k] for k in ("task_id", "canonical_id", "memory_type", "status", "search") if args.get(k)}
        if not filters:
            raise ValueError("list_memories needs task_id, canonical_id, or search")
        recs = client.list_memories(filters, int(args.get("limit", 100)))
        return json.dumps([m.to_dict() for m in recs], indent=2, default=str)
    if name == "get_memory":
        rec = client.get_memory(args["key"])
        return json.dumps(rec.to_dict() if rec else {"found": False}, indent=2, default=str)
    if name == "archive_memory":
        return json.dumps(client.archive_memory(args["key"]), default=str)
    if name == "restore_memory":
        return json.dumps(client.restore_memory(args["key"]), default=str)
    if name == "cairn_howto":
        return howto_text(args.get("topic"))
    if name == "cairn_whoami":
        return json.dumps({"agent": client.agent_id, "embedder": client.embedder.name,
                           "dims": client.embedder.dims,
                           "memories": client.vault.count()}, default=str)
    if name == "cairn_gc":
        return json.dumps(client.gc(dry_run=not bool(args.get("apply", False))), default=str)
    if name == "cairn_export":
        return json.dumps(client.export(args.get("since")), default=str)
    if name == "cairn_import":
        if not isinstance(args.get("pack"), dict):
            raise ValueError("cairn_import needs a pack object from cairn_export")
        return json.dumps(client.import_pack(args["pack"]), default=str)
    if name == "cairn_ingest":
        from cairn.ingest import ingest_dir

        return json.dumps(ingest_dir(client, args["team"], args["dir"],
                                     args.get("memory_type", "document")), default=str)
    raise ValueError(f"unknown tool: {name}")


def respond(msg_id, result=None, error=None) -> None:
    msg: dict = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, default=str) + "\n")
    sys.stdout.flush()


def handle(client: CairnClient, msg: dict) -> bool:
    """Returns False to shut down."""
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        respond(msg_id, {"protocolVersion": PROTOCOL_VERSION,
                         "capabilities": {"tools": {}},
                         "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
    elif method == "notifications/initialized":
        pass  # no response to notifications
    elif method == "ping":
        respond(msg_id, {})
    elif method == "tools/list":
        respond(msg_id, {"tools": TOOL_DEFS})
    elif method == "tools/call":
        params = msg.get("params", {})
        try:
            text = call_tool(client, params.get("name", ""), params.get("arguments", {}))
            respond(msg_id, {"content": [{"type": "text", "text": text}]})
        except (KeyError, ValueError) as e:
            respond(msg_id, error={"code": -32602, "message": str(e)})
        except Exception as e:  # never kill the session on a tool bug
            respond(msg_id, error={"code": -32603, "message": f"{type(e).__name__}: {e}"})
    elif method is not None and msg_id is not None:
        respond(msg_id, error={"code": -32601, "message": f"unknown method: {method}"})
    return True


def main() -> int:
    try:
        client = make_client()
    except (FileNotFoundError, ValueError, ImportError) as e:
        sys.stderr.write(f"cairn-mcp: {e}\n")
        return 2
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


if __name__ == "__main__":
    sys.exit(main())
