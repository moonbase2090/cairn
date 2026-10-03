"""PostgreSQL storage backend using pgvector and PostgreSQL full-text search."""
from __future__ import annotations

from collections.abc import Mapping
import base64
from contextlib import contextmanager
import json
from pathlib import Path
from types import MappingProxyType
import uuid
import time

import numpy as np

from .models import content_digest
from .storage import (
    MEMORY_FIELDS,
    ContentIntegrityError,
    MemoryQuery,
    SpaceMismatchError,
    StorageBackend,
)

READ_COLUMNS = ("rowid", *MEMORY_FIELDS, "content_ref")
SEARCH_FILTER_COLUMNS = ("task_id", "canonical_id", "memory_type", "team_id", "agent_id")
SCHEMA_VERSION = 3
DOC_THRESHOLD = 2048


def _sync_event_order(event: dict) -> tuple[int, int, str, str]:
    return (
        int(event.get("state_revision", 0)), int(event.get("updated_at", 0)),
        str(event.get("state_origin") or event.get("origin_id") or ""),
        str(event.get("state_event_id") or event.get("event_id") or ""),
    )


def _sync_row_order(row) -> tuple[int, int, str, str]:
    return (
        int(row["state_revision"] or 0), int(row["updated_at"] or 0),
        str(row["state_origin"] or ""), str(row["state_event_id"] or ""),
    )


class PostgresVault(StorageBackend):
    """One PostgreSQL database stores one shared Cairn vault."""

    name = "postgres"

    def __init__(self, vault_dir: Path, embed_name: str, dims: int, url: str | None,
                 create: bool = False, doc_threshold: int | None = None):
        if not url:
            raise ValueError("[storage] url is required when backend = 'postgres'")
        try:
            import psycopg
            from pgvector import Vector
            from pgvector.psycopg import register_vector
            from psycopg.rows import dict_row
        except ImportError as e:
            raise ImportError(
                "PostgreSQL storage requires the optional dependencies; install cairn[postgres]"
            ) from e

        self._psycopg = psycopg
        self._Vector = Vector
        self._url = url
        self._embed_name = embed_name
        self._dims = int(dims)
        self._txn = False
        self._vault_dir = Path(vault_dir)
        if create:
            self._vault_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        except psycopg.Error as e:
            message = e.diag.message_primary if e.diag and e.diag.message_primary else str(e)
            raise OSError(f"cannot connect to PostgreSQL storage: {message}") from e

        try:
            self._migrate(create, doc_threshold)
            register_vector(self.conn)
            self._backfill_sync_events()
            self._doc_threshold = int(self._get_meta("doc_threshold") or DOC_THRESHOLD)
        except psycopg.Error as e:
            self.conn.close()
            raise self._storage_error(e) from e
        except Exception:
            self.conn.close()
            raise

    @property
    def vault_dir(self) -> Path:
        return self._vault_dir

    @property
    def doc_threshold(self) -> int:
        return self._doc_threshold

    def reopen(self) -> PostgresVault:
        return PostgresVault(self._vault_dir, self._embed_name, self._dims, self._url)

    @contextmanager
    def transaction(self):
        """Join nested calls to one transaction; commit or roll back as a unit."""
        if self._txn:
            yield
            return
        self._txn = True
        try:
            with self.conn.transaction():
                yield
        except self._psycopg.Error as e:
            raise self._storage_error(e) from e
        finally:
            self._txn = False

    def _execute(self, query: str, params=()):
        try:
            return self.conn.execute(query, params)
        except self._psycopg.Error as e:
            raise self._storage_error(e) from e

    @staticmethod
    def _storage_error(error: Exception) -> OSError:
        diag = getattr(error, "diag", None)
        message = getattr(diag, "message_primary", None) or str(error)
        return OSError(f"PostgreSQL storage error: {message}")

    def _configure_extension_path(self) -> None:
        extension = self._execute("""
            SELECT namespace.nspname AS schema_name
            FROM pg_extension AS extension
            JOIN pg_namespace AS namespace ON namespace.oid=extension.extnamespace
            WHERE extension.extname='vector'
        """).fetchone()
        if extension is None:
            raise RuntimeError("PostgreSQL pgvector extension is not installed")
        schemas = self._execute(
            "SELECT current_schemas(false) AS schemas"
        ).fetchone()["schemas"]
        if not schemas:
            raise RuntimeError("PostgreSQL connection has no usable schema in search_path")
        if extension["schema_name"] not in schemas:
            schemas.append(extension["schema_name"])
        search_path = ", ".join(
            '"' + schema.replace('"', '""') + '"' for schema in schemas
        )
        self._execute("SELECT set_config('search_path', %s, false)", (search_path,))

    def _migration_version(self) -> int:
        exists = self._execute(
            "SELECT to_regclass('cairn_migrations') AS relation_name"
        ).fetchone()["relation_name"] is not None
        if not exists:
            return 0
        rows = self._execute(
            "SELECT version FROM cairn_migrations ORDER BY version"
        ).fetchall()
        return max((row["version"] for row in rows), default=0)

    def _check_space(self) -> None:
        got_model = self._get_meta("embed_model")
        got_dims = self._get_meta("dims")
        if got_model != self._embed_name or got_dims != str(self._dims):
            raise SpaceMismatchError(
                f"vault is {got_model}/{got_dims}d but embedder is "
                f"{self._embed_name}/{self._dims}d — vectors from different spaces "
                "are never compared. Use the original embedder or initialize a new vault."
            )

    def _migrate(self, create: bool, doc_threshold: int | None) -> None:
        exists = self._execute(
            "SELECT to_regclass('cairn_meta') AS relation_name"
        ).fetchone()["relation_name"] is not None
        if not exists and not create:
            raise FileNotFoundError(
                "no Cairn vault in this PostgreSQL database — run `cairn init` first"
            )

        if exists:
            self._configure_extension_path()
            current = self._migration_version()
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"PostgreSQL vault schema {current} is newer than this Cairn version "
                    f"(supports through {SCHEMA_VERSION})"
                )
            if current == SCHEMA_VERSION:
                self._check_space()
                return
            if current == 0 and not create:
                raise FileNotFoundError(
                    "no Cairn vault in this PostgreSQL database — run `cairn init` first"
                )

        # Only schema creation or an upgrade takes this database-level lock.
        with self.transaction():
            self._execute("SELECT pg_advisory_xact_lock(%s)", (0x434149524E,))
            self._execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
            self._configure_extension_path()
            self._execute("""
                CREATE TABLE IF NOT EXISTS cairn_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            self._execute("""
                CREATE TABLE IF NOT EXISTS cairn_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)

            current = self._migration_version()
            if current == 0 and not create:
                raise FileNotFoundError(
                    "no Cairn vault in this PostgreSQL database — run `cairn init` first"
                )
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"PostgreSQL vault schema {current} is newer than this Cairn version "
                    f"(supports through {SCHEMA_VERSION})"
                )
            for version in range(current + 1, SCHEMA_VERSION + 1):
                self._apply_migration(version)
                self._execute("INSERT INTO cairn_migrations(version) VALUES (%s)", (version,))

            if current == 0:
                self._set_meta("embed_model", self._embed_name)
                self._set_meta("dims", str(self._dims))
                self._set_meta(
                    "doc_threshold",
                    str(DOC_THRESHOLD if doc_threshold is None else doc_threshold),
                )
            self._check_space()
            self._set_meta("schema_version", str(SCHEMA_VERSION))

    def _apply_migration(self, version: int) -> None:
        if version == 2:
            self._execute("""
                CREATE TABLE cairn_server_tokens (
                    token_id TEXT PRIMARY KEY,
                    token_hash TEXT UNIQUE NOT NULL,
                    agent_id TEXT NOT NULL,
                    curator BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at BIGINT NOT NULL
                )
            """)
            return
        if version == 3:
            self._execute("ALTER TABLE cairn_memories ADD COLUMN IF NOT EXISTS state_revision BIGINT NOT NULL DEFAULT 1")
            self._execute("ALTER TABLE cairn_memories ADD COLUMN IF NOT EXISTS state_origin TEXT NOT NULL DEFAULT ''")
            self._execute("ALTER TABLE cairn_memories ADD COLUMN IF NOT EXISTS state_event_id TEXT NOT NULL DEFAULT ''")
            self._execute("ALTER TABLE cairn_server_tokens ADD COLUMN IF NOT EXISTS curator BOOLEAN NOT NULL DEFAULT FALSE")
            self._execute("""
                CREATE TABLE cairn_sync_events(
                    feed_seq BIGINT PRIMARY KEY,
                    event_id TEXT UNIQUE NOT NULL,
                    event_json JSONB NOT NULL
                )
            """)
            self._execute("""
                CREATE TABLE cairn_sync_tombstones(
                    key TEXT PRIMARY KEY, event_json JSONB NOT NULL
                )
            """)
            self._execute("""
                CREATE TABLE cairn_sync_cursors(
                    peer TEXT NOT NULL, direction TEXT NOT NULL, cursor BIGINT NOT NULL,
                    updated_at BIGINT NOT NULL, PRIMARY KEY(peer, direction)
                )
            """)
            self._execute("""
                CREATE TABLE cairn_sync_conflicts(
                    base_key TEXT PRIMARY KEY, competitor_keys JSONB NOT NULL,
                    detected_at BIGINT NOT NULL, resolved_at BIGINT, winner_key TEXT,
                    resolved_by TEXT
                )
            """)
            return
        if version != 1:
            raise RuntimeError(f"missing PostgreSQL schema migration {version}")
        self._execute(f"""
            CREATE TABLE cairn_memories (
                rowid BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                key TEXT UNIQUE NOT NULL,
                canonical_id TEXT NOT NULL,
                content TEXT,
                content_ref TEXT,
                content_summary TEXT,
                memory_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                origin TEXT NOT NULL DEFAULT 'agent',
                task_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                team_id TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                state_revision INTEGER NOT NULL DEFAULT 1,
                state_origin TEXT NOT NULL DEFAULT '',
                state_event_id TEXT NOT NULL DEFAULT '',
                created_at BIGINT NOT NULL,
                updated_at BIGINT NOT NULL,
                expires_at BIGINT,
                archived_at BIGINT,
                supersedes TEXT,
                parent_key TEXT,
                provenance TEXT,
                confidence DOUBLE PRECISION,
                content_hash TEXT NOT NULL,
                embedding vector({self._dims}) NOT NULL,
                CHECK ((content IS NULL) <> (content_ref IS NULL))
            )
        """)
        self._execute("CREATE INDEX idx_cairn_mem_task ON cairn_memories(task_id, created_at)")
        self._execute("CREATE INDEX idx_cairn_mem_canon ON cairn_memories(canonical_id)")
        self._execute("CREATE INDEX idx_cairn_mem_hash ON cairn_memories(content_hash)")
        self._execute("CREATE INDEX idx_cairn_mem_status ON cairn_memories(status)")
        self._execute("CREATE INDEX idx_cairn_mem_ref ON cairn_memories(content_ref)")
        self._execute("""
            CREATE TABLE cairn_documents (
                content_hash TEXT PRIMARY KEY,
                content TEXT NOT NULL
            )
        """)
        self._execute("""
            CREATE TABLE cairn_search (
                key TEXT PRIMARY KEY REFERENCES cairn_memories(key) ON DELETE CASCADE,
                terms TSVECTOR NOT NULL
            )
        """)
        self._execute(
            "CREATE INDEX idx_cairn_search_terms ON cairn_search USING GIN(terms)"
        )

    def _get_meta(self, key: str) -> str | None:
        row = self._execute("SELECT value FROM cairn_meta WHERE key=%s", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._execute("""
            INSERT INTO cairn_meta(key, value) VALUES (%s, %s)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
        """, (key, value))

    def _row(self, row: Mapping | None) -> Mapping | None:
        if row is None:
            return None
        values = dict(row)
        if "embedding" in values:
            embedding = values["embedding"]
            if hasattr(embedding, "to_numpy"):
                embedding = embedding.to_numpy()
            values["embedding"] = np.asarray(embedding, dtype=np.float32).tobytes()
        return MappingProxyType(values)

    def _new_sync_event(self, kind: str, key: str, revision: int, updated_at: int) -> dict:
        origin = self._get_meta("sync_origin_id")
        seq = self._execute(
            "INSERT INTO cairn_meta(key, value) VALUES('sync_origin_seq', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=(cairn_meta.value::bigint + 1)::text "
            "RETURNING value"
        ).fetchone()["value"]
        seq = int(seq)
        return {
            "event_id": f"{origin}:{seq}", "origin_id": origin, "origin_seq": seq,
            "kind": kind, "key": key, "state_revision": revision,
            "updated_at": updated_at, "state_origin": origin,
            "state_event_id": f"{origin}:{seq}",
        }

    def _append_sync_event(self, event: dict) -> None:
        feed_seq = int(self._execute(
            "INSERT INTO cairn_meta(key, value) VALUES('sync_feed_seq', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=(cairn_meta.value::bigint + 1)::text "
            "RETURNING value"
        ).fetchone()["value"])
        self._execute(
            "INSERT INTO cairn_sync_events(feed_seq, event_id, event_json) "
            "VALUES(%s, %s, %s::jsonb) "
            "ON CONFLICT(event_id) DO NOTHING",
            (feed_seq, event["event_id"],
             json.dumps(event, separators=(",", ":"), default=str)),
        )

    def _backfill_sync_events(self) -> None:
        with self.transaction():
            self._execute("SELECT pg_advisory_xact_lock(%s)", (0x434149524E,))
            origin = self._get_meta("sync_origin_id")
            if not origin:
                origin = uuid.uuid4().hex
                self._set_meta("sync_origin_id", origin)
            if self._get_meta("sync_origin_seq") is None:
                self._set_meta("sync_origin_seq", "0")
            if self._get_meta("sync_feed_seq") is None:
                self._set_meta("sync_feed_seq", "0")
            rows = self._execute(
                f"SELECT {', '.join(READ_COLUMNS)}, embedding FROM cairn_memories "
                "WHERE state_origin='' OR state_event_id='' ORDER BY rowid"
            ).fetchall()
            for row in rows:
                event = self._new_sync_event("snapshot", row["key"],
                                             int(row["state_revision"]), int(row["updated_at"]))
                snapshot = {field: row[field] for field in MEMORY_FIELDS}
                snapshot["content"] = self.read_content(row)
                embedding = row["embedding"]
                if hasattr(embedding, "to_numpy"):
                    embedding = embedding.to_numpy()
                snapshot["embedding"] = base64.b64encode(
                    np.asarray(embedding, dtype=np.float32).tobytes()
                ).decode("ascii")
                snapshot["state_origin"] = origin
                snapshot["state_event_id"] = event["event_id"]
                event["snapshot"] = snapshot
                self._execute(
                    "UPDATE cairn_memories SET state_origin=%s, state_event_id=%s WHERE key=%s",
                    (origin, event["event_id"], row["key"]),
                )
                self._append_sync_event(event)

    def _rows(self, rows) -> list[Mapping]:
        return [self._row(row) for row in rows]

    def insert(self, rec: dict, vector: np.ndarray) -> int:
        rec = dict(rec)
        text = rec["content"]
        digest = rec.get("content_hash") or f"sha256:{content_digest(text)}"
        vector = np.asarray(vector, dtype=np.float32).ravel()
        if vector.size != self._dims:
            raise ValueError(f"embedding is {vector.size}d, vault is {self._dims}d")
        if len(text.encode("utf-8")) > self._doc_threshold:
            content, ref = None, digest
        else:
            content, ref = text, None

        with self.transaction():
            local_event = None
            if not rec.get("state_event_id"):
                revision = int(rec.get("state_revision", 1))
                local_event = self._new_sync_event(
                    "snapshot", rec["key"], revision, int(rec["updated_at"]),
                )
                rec.update({"state_revision": revision, "state_origin": local_event["origin_id"],
                            "state_event_id": local_event["event_id"]})
            fields = (
                "key", "canonical_id", "content", "content_ref", "content_summary",
                "memory_type", "status", "origin", "task_id", "agent_id", "team_id",
                "version", "state_revision", "state_origin", "state_event_id",
                "created_at", "updated_at", "expires_at", "archived_at", "supersedes",
                "parent_key", "provenance", "confidence", "content_hash", "embedding",
            )
            values = [
                rec["key"], rec["canonical_id"], content, ref, rec["content_summary"],
                rec["memory_type"], rec["status"], rec["origin"], rec["task_id"],
                rec["agent_id"], rec["team_id"], rec["version"], rec["state_revision"],
                rec["state_origin"], rec["state_event_id"], rec["created_at"],
                rec["updated_at"], rec["expires_at"], rec["archived_at"], rec["supersedes"],
                rec["parent_key"], rec["provenance"], rec["confidence"], digest,
                self._Vector(vector.tolist()),
            ]
            placeholders = ", ".join("%s" for _ in fields)
            if ref is not None:
                self._execute("""
                    INSERT INTO cairn_documents(content_hash, content) VALUES (%s, %s)
                    ON CONFLICT(content_hash) DO NOTHING
                """, (digest, text))
                stored = self._execute(
                    "SELECT content FROM cairn_documents WHERE content_hash=%s", (digest,)
                ).fetchone()["content"]
                if stored != text:
                    raise ContentIntegrityError(f"content hash collision for {digest}")
            row = self._execute(
                f"INSERT INTO cairn_memories({', '.join(fields)}) "
                f"VALUES ({placeholders}) RETURNING rowid",
                values,
            ).fetchone()
            self._execute(
                "INSERT INTO cairn_search(key, terms) VALUES (%s, to_tsvector('simple', %s))",
                (rec["key"], text),
            )
            if local_event is not None:
                snapshot = {field: rec.get(field) for field in MEMORY_FIELDS}
                snapshot["content"] = text
                snapshot["embedding"] = base64.b64encode(vector.tobytes()).decode("ascii")
                local_event["snapshot"] = snapshot
                self._append_sync_event(local_event)
        return row["rowid"]

    def get(self, key: str) -> Mapping | None:
        cols = ", ".join(READ_COLUMNS)
        row = self._execute(
            f"SELECT {cols} FROM cairn_memories WHERE key=%s", (key,)
        ).fetchone()
        return self._row(row)

    def by_hash(self, content_hash: str, task_id: str, status: str = "active") -> list[Mapping]:
        cols = ", ".join(READ_COLUMNS)
        rows = self._execute(
            f"SELECT {cols} FROM cairn_memories "
            "WHERE content_hash=%s AND task_id=%s AND status=%s",
            (content_hash, task_id, status),
        ).fetchall()
        return self._rows(rows)

    def create_server_token(self, token_id: str, token_hash: str, agent_id: str,
                            created_at: int, curator: bool = False) -> None:
        self._execute(
            "INSERT INTO cairn_server_tokens(token_id, token_hash, agent_id, curator, created_at) "
            "VALUES(%s, %s, %s, %s, %s)",
            (token_id, token_hash, agent_id, curator, created_at),
        )

    def get_server_token(self, token_hash: str) -> Mapping | None:
        return self._execute(
            "SELECT token_id, agent_id, curator, created_at FROM cairn_server_tokens WHERE token_hash=%s",
            (token_hash,),
        ).fetchone()

    def list_server_tokens(self) -> list[Mapping]:
        return self._execute(
            "SELECT token_id, agent_id, curator, created_at FROM cairn_server_tokens "
            "ORDER BY created_at, token_id"
        ).fetchall()

    def delete_server_token(self, token_id: str) -> int:
        return self._execute(
            "DELETE FROM cairn_server_tokens WHERE token_id=%s", (token_id,)
        ).rowcount

    def get_agent_ids(self, keys: list[str]) -> dict[str, str]:
        if not keys:
            return {}
        rows = self._execute(
            "SELECT key, agent_id FROM cairn_memories WHERE key = ANY(%s)", (keys,)
        ).fetchall()
        return {row["key"]: row["agent_id"] for row in rows}

    def sync_origin_id(self) -> str:
        return self._get_meta("sync_origin_id") or ""

    def export_sync_events(self, after: int = 0, limit: int = 100_000) -> dict:
        rows = self._execute(
            "SELECT feed_seq, event_json FROM cairn_sync_events WHERE feed_seq>%s "
            "ORDER BY feed_seq LIMIT %s",
            (after, limit + 1),
        ).fetchall()
        rows = rows[:limit]
        events = [{**row["event_json"], "feed_seq": row["feed_seq"]} for row in rows]
        cursor = rows[-1]["feed_seq"] if rows else after
        return {"pack": "cairn-sync-2", "after": after, "cursor": cursor,
                "events": events}

    def has_sync_event(self, event_id: str) -> bool:
        return self._execute(
            "SELECT 1 FROM cairn_sync_events WHERE event_id=%s", (event_id,),
        ).fetchone() is not None

    def has_sync_tombstone(self, key: str) -> bool:
        return self._execute(
            "SELECT 1 FROM cairn_sync_tombstones WHERE key=%s", (key,),
        ).fetchone() is not None

    def _remember_tombstone(self, event: dict) -> None:
        self._execute(
            "INSERT INTO cairn_sync_tombstones(key, event_json) VALUES(%s, %s::jsonb) "
            "ON CONFLICT(key) DO UPDATE SET event_json=EXCLUDED.event_json",
            (event["key"], json.dumps(event, separators=(",", ":"), default=str)),
        )

    def apply_sync_event(self, event: dict) -> str:
        if not isinstance(event, dict) or not isinstance(event.get("event_id"), str):
            raise ValueError("invalid sync event: missing event_id")
        with self.transaction():
            duplicate = self._execute(
                "SELECT 1 FROM cairn_sync_events WHERE event_id=%s", (event["event_id"],),
            ).fetchone()
            if duplicate:
                return "skipped"
            kind, key = event.get("kind"), event.get("key")
            if kind not in {"snapshot", "state", "tombstone"} or not isinstance(key, str):
                raise ValueError("invalid sync event kind or key")
            row = self._execute(
                f"SELECT {', '.join(READ_COLUMNS)}, embedding FROM cairn_memories "
                "WHERE key=%s FOR UPDATE",
                (key,),
            ).fetchone()
            existing = self._row(row)
            resolution = event.get("conflict_resolution")
            if resolution is not None and not (kind == "state" and existing is None
                                                and self.has_sync_tombstone(key)):
                self._validate_resolution_event(event, resolution)
            incoming_order = _sync_event_order(event)
            current_order = _sync_row_order(existing) if existing else None
            changed = False
            if kind == "snapshot":
                snapshot = event.get("snapshot")
                if not isinstance(snapshot, dict) or snapshot.get("key") != key:
                    raise ValueError("invalid snapshot sync event")
                if existing and existing["content_hash"] != snapshot.get("content_hash"):
                    raise ValueError(f"sync integrity conflict for {key}: content hash changed")
                if not existing:
                    if self.has_sync_tombstone(key):
                        self._append_sync_event(event)
                        return "skipped"
                    raw = snapshot.get("embedding")
                    if not isinstance(raw, str):
                        raise ValueError(f"sync snapshot for {key} has no embedding")
                    rec = {field: snapshot.get(field) for field in MEMORY_FIELDS}
                    rec["content"] = snapshot.get("content")
                    self.insert(rec, np.frombuffer(base64.b64decode(raw), dtype=np.float32).copy())
                    changed = True
                elif incoming_order > current_order:
                    self._execute(
                        "UPDATE cairn_memories SET status=%s, updated_at=%s, archived_at=%s, "
                        "expires_at=%s, state_revision=%s, state_origin=%s, state_event_id=%s "
                        "WHERE key=%s",
                        (snapshot.get("status"), event.get("updated_at"), snapshot.get("archived_at"),
                         snapshot.get("expires_at"), event.get("state_revision"),
                         event.get("state_origin", event.get("origin_id")),
                         event.get("state_event_id", event["event_id"]), key),
                    )
                    changed = True
            elif kind == "state":
                if existing is None:
                    if self.has_sync_tombstone(key):
                        self._append_sync_event(event)
                        return "skipped"
                    raise ValueError(f"state event for missing key {key}; send a snapshot first")
                if incoming_order > current_order:
                    self._execute(
                        "UPDATE cairn_memories SET status=%s, updated_at=%s, archived_at=%s, "
                        "state_revision=%s, state_origin=%s, state_event_id=%s WHERE key=%s",
                        (event.get("status"), event.get("updated_at"), event.get("archived_at"),
                         event.get("state_revision"), event.get("state_origin", event.get("origin_id")),
                         event.get("state_event_id", event["event_id"]), key),
                    )
                    changed = True
            else:
                self._remember_tombstone(event)
                if existing is not None:
                    self._execute("DELETE FROM cairn_memories WHERE key=%s", (key,))
                    self._delete_orphan_docs()
                    changed = True
            self._append_sync_event(event)
            if changed:
                self._record_resolution_event(event)
        return "added" if kind == "snapshot" and changed and existing is None else (
            "updated" if changed else "skipped")

    def _record_resolution_event(self, event: dict) -> None:
        resolution = event.get("conflict_resolution")
        if not isinstance(resolution, dict):
            return
        base_key = resolution.get("base_key")
        competitors = resolution.get("competitor_keys")
        if not isinstance(base_key, str) or not isinstance(competitors, list):
            return
        resolved_at = int(event.get("updated_at", 0))
        self._execute(
            "INSERT INTO cairn_sync_conflicts(base_key, competitor_keys, detected_at, resolved_at, "
            "winner_key, resolved_by) VALUES(%s, %s::jsonb, %s, %s, %s, %s) "
            "ON CONFLICT(base_key) DO UPDATE SET competitor_keys=EXCLUDED.competitor_keys, "
            "resolved_at=EXCLUDED.resolved_at, winner_key=EXCLUDED.winner_key, "
            "resolved_by=EXCLUDED.resolved_by",
            (base_key, json.dumps(competitors), resolved_at, resolved_at,
             resolution.get("winner_key"), resolution.get("resolved_by") or "sync"),
        )

    def _validate_resolution_event(self, event: dict, resolution: dict) -> None:
        if (event.get("kind") != "state" or event.get("status") != "superseded"
                or not isinstance(resolution, dict)):
            raise ValueError("conflict resolution must be a supersede state event")
        base_key = resolution.get("base_key")
        winner_key = resolution.get("winner_key")
        competitors = resolution.get("competitor_keys")
        if (not isinstance(base_key, str) or not isinstance(winner_key, str)
                or not isinstance(competitors, list) or len(competitors) < 2
                or any(not isinstance(key, str) for key in competitors)
                or len(set(competitors)) != len(competitors)
                or not isinstance(resolution.get("resolved_by"), str)
                or not resolution["resolved_by"]
                or event.get("key") not in competitors or winner_key not in competitors
                or event.get("key") == winner_key):
            raise ValueError("invalid competing correction resolution")
        stored = next((item for item in self.list_sync_conflicts(include_resolved=True)
                       if item["base_key"] == base_key
                       and set(item["competitor_keys"]) == set(competitors)), None)
        dynamic = next((item for item in self.list_competing_corrections()
                        if item["base_key"] == base_key
                        and set(item["competitor_keys"]) == set(competitors)), None)
        winner = self.get(winner_key)
        parents_match = all(
            (row := self.get(key)) is not None and row["supersedes"] == base_key
            for key in competitors
        )
        if ((stored is None and dynamic is None) or winner is None
                or winner["status"] != "active" or not parents_match):
            raise ValueError("conflict resolution does not match an active competing correction")

    def get_sync_cursor(self, peer: str, direction: str) -> int:
        row = self._execute(
            "SELECT cursor FROM cairn_sync_cursors WHERE peer=%s AND direction=%s",
            (peer, direction),
        ).fetchone()
        return int(row["cursor"]) if row else 0

    def list_sync_cursors(self) -> list[dict]:
        return [dict(row) for row in self._execute(
            "SELECT peer, direction, cursor, updated_at FROM cairn_sync_cursors "
            "ORDER BY peer, direction"
        ).fetchall()]

    def set_sync_cursor(self, peer: str, direction: str, cursor: int, now: int) -> None:
        if direction not in {"push", "pull"} or cursor < 0:
            raise ValueError("invalid sync cursor")
        self._execute(
            "INSERT INTO cairn_sync_cursors(peer, direction, cursor, updated_at) "
            "VALUES(%s, %s, %s, %s) ON CONFLICT(peer, direction) DO UPDATE "
            "SET cursor=GREATEST(cairn_sync_cursors.cursor, EXCLUDED.cursor), "
            "updated_at=EXCLUDED.updated_at",
            (peer, direction, cursor, now),
        )

    def list_competing_corrections(self) -> list[dict]:
        rows = self._execute(
            "SELECT supersedes AS base_key, array_agg(key ORDER BY key) AS keys "
            "FROM cairn_memories WHERE status='active' AND supersedes IS NOT NULL "
            "GROUP BY supersedes HAVING COUNT(*) > 1 ORDER BY supersedes"
        ).fetchall()
        return [{"base_key": row["base_key"], "competitor_keys": row["keys"]} for row in rows]

    def record_competing_corrections(self, now: int) -> list[dict]:
        conflicts = self.list_competing_corrections()
        for item in conflicts:
            self._execute(
                "INSERT INTO cairn_sync_conflicts(base_key, competitor_keys, detected_at) "
                "VALUES(%s, %s::jsonb, %s) ON CONFLICT(base_key) DO UPDATE SET "
                "competitor_keys=EXCLUDED.competitor_keys, detected_at=CASE WHEN "
                "cairn_sync_conflicts.resolved_at IS NULL THEN cairn_sync_conflicts.detected_at "
                "ELSE EXCLUDED.detected_at END, resolved_at=NULL, winner_key=NULL, resolved_by=NULL",
                (item["base_key"], json.dumps(item["competitor_keys"]), now),
            )
        unresolved = self._execute(
            "SELECT base_key, competitor_keys FROM cairn_sync_conflicts WHERE resolved_at IS NULL"
        ).fetchall()
        for row in unresolved:
            keys = row["competitor_keys"]
            if isinstance(keys, str):
                keys = json.loads(keys)
            if not keys:
                continue
            active = self._execute(
                "SELECT key FROM cairn_memories WHERE status='active' AND key = ANY(%s)",
                (keys,),
            ).fetchall()
            if len(active) == 1:
                self._execute(
                    "UPDATE cairn_sync_conflicts SET resolved_at=%s, winner_key=%s, "
                    "resolved_by='sync' WHERE base_key=%s AND resolved_at IS NULL",
                    (now, active[0]["key"], row["base_key"]),
                )
        return conflicts

    def list_sync_conflicts(self, include_resolved: bool = False) -> list[dict]:
        suffix = "" if include_resolved else " WHERE resolved_at IS NULL"
        rows = self._execute(
            "SELECT * FROM cairn_sync_conflicts" + suffix + " ORDER BY detected_at, base_key"
        ).fetchall()
        return [{**row, "resolved": row["resolved_at"] is not None} for row in rows]

    def resolve_sync_conflict(self, base_key: str, winner_key: str,
                              resolved_by: str, now: int) -> None:
        row = self._execute(
            "SELECT competitor_keys FROM cairn_sync_conflicts WHERE base_key=%s", (base_key,),
        ).fetchone()
        if row is None:
            raise KeyError(f"no recorded competing correction for {base_key}")
        if winner_key not in row["competitor_keys"]:
            raise ValueError("winner must be one of the competing corrections")
        self._execute(
            "UPDATE cairn_sync_conflicts SET resolved_at=%s, winner_key=%s, resolved_by=%s "
            "WHERE base_key=%s", (now, winner_key, resolved_by, base_key),
        )

    def find(self, query: MemoryQuery, limit: int = 100,
             with_embedding: bool = False) -> list[Mapping]:
        columns = READ_COLUMNS + (("embedding",) if with_embedding else ())
        clauses = [f"{column}=%s" for column in query.eq]
        args = list(query.eq.values())
        for field, sql in (
            ("exclude_key", "key<>%s"),
            ("updated_before", "updated_at<=%s"),
            ("updated_since", "updated_at>=%s"),
            ("archived_before", "archived_at IS NOT NULL AND archived_at<=%s"),
            ("expired_by", "expires_at IS NOT NULL AND expires_at<=%s"),
        ):
            value = getattr(query, field)
            if value is not None:
                clauses.append(sql)
                args.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(limit)
        rows = self._execute(
            f"SELECT {', '.join(columns)} FROM cairn_memories{where} "
            f"ORDER BY created_at ASC LIMIT %s",
            args,
        ).fetchall()
        return self._rows(rows)

    def set_status(self, key: str, status: str, now: int,
                   archived_at: int | None = None,
                   event_metadata: dict | None = None) -> int:
        with self.transaction():
            row = self._execute(
                "SELECT state_revision FROM cairn_memories WHERE key=%s FOR UPDATE", (key,),
            ).fetchone()
            if row is None:
                return 0
            event = self._new_sync_event("state", key, int(row["state_revision"]) + 1, now)
            event.update({"status": status, "archived_at": archived_at})
            event.update(event_metadata or {})
            if event.get("conflict_resolution") is not None:
                self._validate_resolution_event(event, event["conflict_resolution"])
            cur = self._execute(
                "UPDATE cairn_memories SET status=%s, updated_at=%s, archived_at=%s, "
                "state_revision=%s, state_origin=%s, state_event_id=%s WHERE key=%s",
                (status, now, archived_at, event["state_revision"], event["state_origin"],
                 event["event_id"], key),
            )
            self._append_sync_event(event)
            self._record_resolution_event(event)
        return cur.rowcount

    def delete_by_keys(self, keys: list[str], reason: str = "deleted") -> int:
        if not keys:
            return 0
        placeholders = ", ".join("%s" for _ in keys)
        with self.transaction():
            rows = self._execute(
                f"SELECT key, canonical_id, agent_id, state_revision FROM cairn_memories "
                f"WHERE key IN ({placeholders}) FOR UPDATE", keys,
            ).fetchall()
            for row in rows:
                event = self._new_sync_event(
                    "tombstone", row["key"], int(row["state_revision"]) + 1, int(time.time()),
                )
                event.update({"canonical_id": row["canonical_id"],
                              "agent_id": row["agent_id"], "reason": reason})
                self._append_sync_event(event)
                self._remember_tombstone(event)
            rows = self._execute(
                f"DELETE FROM cairn_memories WHERE key IN ({placeholders}) "
                "RETURNING content_hash",
                keys,
            ).fetchall()
            self._delete_orphan_docs()
        return len(rows)

    def delete_by_canonical(self, canonical_id: str, reason: str = "purged") -> int:
        with self.transaction():
            rows = self._execute(
                "SELECT key FROM cairn_memories WHERE canonical_id=%s", (canonical_id,)
            ).fetchall()
            return self.delete_by_keys([row["key"] for row in rows], reason)

    def count(self) -> int:
        return self._execute("SELECT COUNT(*) AS count FROM cairn_memories").fetchone()["count"]

    def count_by_status(self) -> dict[str, int]:
        rows = self._execute(
            "SELECT status, COUNT(*) AS count FROM cairn_memories GROUP BY status"
        ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def fts_search(self, text: str, limit: int = 100, status: str = "active",
                   extra: dict | None = None) -> list[Mapping]:
        if not text.strip():
            return []
        clauses = ["s.terms @@ q.terms", "m.status=%s"]
        args = [text, status]
        for column in SEARCH_FILTER_COLUMNS:
            if extra and extra.get(column) is not None:
                clauses.append(f"m.{column}=%s")
                args.append(extra[column])
        args.append(limit)
        rows = self._execute(f"""
            WITH q AS (SELECT plainto_tsquery('simple', %s) AS terms)
            SELECT {', '.join(f'm.{column}' for column in READ_COLUMNS)},
                   ts_rank(s.terms, q.terms) AS _rank
            FROM cairn_search AS s
            JOIN cairn_memories AS m ON m.key=s.key
            CROSS JOIN q
            WHERE {' AND '.join(clauses)}
            ORDER BY _rank DESC, m.created_at ASC
            LIMIT %s
        """, args).fetchall()
        return self._rows(rows)

    def knn(self, query: np.ndarray, k: int, status: str = "active",
            filters: dict | None = None, now: int | None = None) -> list[tuple[Mapping, float]]:
        if k <= 0:
            return []
        vector = np.asarray(query, dtype=np.float32).ravel()
        if vector.size != self._dims:
            raise ValueError(f"query is {vector.size}d, vault is {self._dims}d")
        clauses = ["status=%s"]
        filter_values = [status]
        for column in ("task_id", "memory_type", "team_id", "agent_id"):
            if filters and filters.get(column) is not None:
                clauses.append(f"{column}=%s")
                filter_values.append(filters[column])
        if now is not None:
            clauses.append("(expires_at IS NULL OR expires_at>%s)")
            filter_values.append(now)
        vector_param = self._Vector(vector.tolist())
        rows = self._execute(f"""
            SELECT {', '.join(READ_COLUMNS)},
                   COALESCE(embedding <=> %s, 1.0) AS _distance
            FROM cairn_memories
            WHERE {' AND '.join(clauses)}
            ORDER BY _distance ASC, created_at ASC
            LIMIT %s
        """, (vector_param, *filter_values, k)).fetchall()
        return [(self._row(row), float(row["_distance"])) for row in rows]

    def vec_status(self) -> dict:
        row = self._execute("""
            SELECT COUNT(*) AS memories, COUNT(embedding) AS vectors
            FROM cairn_memories
        """).fetchone()
        return {
            "vec_extension": True,
            "vec_rows": row["vectors"],
            "vec_in_sync": row["vectors"] == row["memories"],
        }

    def rebuild_vec(self) -> dict:
        # Vectors live in the memory rows; PostgreSQL has no derived vector table to rebuild.
        return {"rebuilt": 0}

    def read_content(self, row: Mapping) -> str:
        if row["content"] is not None:
            return row["content"]
        ref = row["content_ref"]
        if ref is None:
            raise ContentIntegrityError(f"content reference is missing for {row['key']}")
        doc = self._execute(
            "SELECT content FROM cairn_documents WHERE content_hash=%s", (ref,)
        ).fetchone()
        if doc is None:
            raise ContentIntegrityError(
                f"document missing for {row['key']}: expected shared PostgreSQL document {ref}"
            )
        text = doc["content"]
        if f"sha256:{content_digest(text)}" != ref:
            raise ContentIntegrityError(
                f"document for {row['key']} fails its hash check ({ref})"
            )
        return text

    def _delete_orphan_docs(self) -> int:
        rows = self._execute("""
            DELETE FROM cairn_documents AS d
            WHERE NOT EXISTS (
                SELECT 1 FROM cairn_memories AS m WHERE m.content_ref=d.content_hash
            )
            RETURNING content_hash
        """).fetchall()
        return len(rows)

    def sweep_orphan_docs(self) -> int:
        with self.transaction():
            return self._delete_orphan_docs()

    def doc_stats(self) -> dict:
        row = self._execute("""
            SELECT COUNT(*) AS files,
                   COALESCE(SUM(octet_length(content)), 0) AS bytes
            FROM cairn_documents
        """).fetchone()
        return {"files": row["files"], "bytes": row["bytes"]}

    def close(self) -> None:
        if not self.conn.closed:
            self.conn.close()
