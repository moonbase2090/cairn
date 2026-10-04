"""SQLite-backed vault: metadata table + sqlite-vec ANN index (brute-force fallback).

One file per vault: `<dir>/vault.db`. The collection is tagged with its embed
model + dims at init; opening with a mismatched embedder is refused so vector
spaces are never mixed.
"""
from __future__ import annotations

import logging
import base64
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from .storage import (
    ContentIntegrityError,
    MEMORY_FIELDS,
    MemoryQuery,
    SpaceMismatchError,
    StorageBackend,
)
from .vault_identity import (
    VaultIdentity,
    ensure_vault_identity,
    load_vault_identity,
    split_legacy_cursor_peer,
)

log = logging.getLogger(__name__)

try:
    import sqlite_vec  # type: ignore

    _HAS_VEC = True
except ImportError:  # pragma: no cover
    _HAS_VEC = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memories(
  rowid INTEGER PRIMARY KEY,
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
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  expires_at INTEGER,
  archived_at INTEGER,
  supersedes TEXT,
  parent_key TEXT,
  provenance TEXT,
  confidence REAL,
  content_hash TEXT NOT NULL,
  embedding BLOB NOT NULL,
  CHECK ((content IS NULL) != (content_ref IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_mem_task ON memories(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_mem_canon ON memories(canonical_id);
CREATE INDEX IF NOT EXISTS idx_mem_hash ON memories(content_hash);
CREATE INDEX IF NOT EXISTS idx_mem_status ON memories(status);
CREATE TABLE IF NOT EXISTS server_tokens(
  token_id TEXT PRIMARY KEY,
  token_hash TEXT UNIQUE NOT NULL,
  agent_id TEXT NOT NULL,
  curator INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT UNIQUE NOT NULL,
  event_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_tombstones(
  key TEXT PRIMARY KEY,
  event_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_cursors(
  peer TEXT NOT NULL,
  direction TEXT NOT NULL,
  cursor INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(peer, direction)
);
CREATE TABLE IF NOT EXISTS sync_conflicts(
  base_key TEXT PRIMARY KEY,
  competitor_keys TEXT NOT NULL,
  detected_at INTEGER NOT NULL,
  resolved_at INTEGER,
  winner_key TEXT,
  resolved_by TEXT
);
"""

# BM25 keyword index (SQLite FTS5 — stdlib, offline, zero new deps). Joined back
# to memories by rowid so status/type/team filters stay exact SQL; triggers keep
# it in sync on every write path (store, import, purge, gc).
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(
  key UNINDEXED, content, tokenize='unicode61 remove_diacritics 1');
CREATE TRIGGER IF NOT EXISTS mem_fts_ai AFTER INSERT ON memories WHEN NEW.content IS NOT NULL BEGIN
  INSERT INTO mem_fts(rowid, key, content) VALUES (new.rowid, new.key, new.content);
END;
CREATE TRIGGER IF NOT EXISTS mem_fts_ad AFTER DELETE ON memories BEGIN
  DELETE FROM mem_fts WHERE rowid = old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS mem_fts_au AFTER UPDATE OF content ON memories WHEN NEW.content IS NOT NULL BEGIN
  DELETE FROM mem_fts WHERE rowid = old.rowid;
  INSERT INTO mem_fts(rowid, key, content) VALUES (new.rowid, new.key, new.content);
END;
"""

COLUMNS = [
    "rowid", "key", "canonical_id", "content", "content_ref", "content_summary", "memory_type",
    "status", "origin", "task_id", "agent_id", "team_id", "version",
    "state_revision", "state_origin", "state_event_id",
    "created_at", "updated_at", "expires_at", "archived_at", "supersedes",
    "parent_key",     "provenance", "confidence", "content_hash", "embedding",
]
# MemoryQuery bounds: field name -> SQL condition on one bound value.
QUERY_BOUNDS = (
    ("exclude_key", "key!=?"),
    ("updated_before", "updated_at<=?"),
    ("updated_since", "updated_at>=?"),
    ("archived_before", "archived_at IS NOT NULL AND archived_at<=?"),
    ("expired_by", "expires_at IS NOT NULL AND expires_at<=?"),
)
# Reads that do not score vectors skip the embedding blob.
READ_COLUMNS = [c for c in COLUMNS if c != "embedding"]

DEFAULT_DOC_THRESHOLD = 2048  # memories larger than this spill content to docs/


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


class Vault(StorageBackend):
    """The default `sqlite` storage backend."""

    name = "sqlite"

    def __init__(self, db_path: str | Path, embed_name: str, dims: int, create: bool = False,
                 doc_threshold: int | None = None):
        self.db_path = Path(db_path)
        self._embed_name, self._dims = embed_name, dims
        if not create and not self.db_path.exists():
            raise FileNotFoundError(
                f"no vault at {self.db_path} — run `cairn init` first"
            )
        if create:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA mmap_size=268435456")
        self.conn.execute("PRAGMA cache_size=-8000")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._vec = False
        if _HAS_VEC:
            try:
                self.conn.enable_load_extension(True)
                sqlite_vec.load(self.conn)
                self._vec = True
            except sqlite3.Error:
                self._vec = False
        self.conn.executescript(SCHEMA)
        self.docs_root = self.db_path.parent / "docs"
        self._fts = True
        try:
            self.conn.executescript(FTS_SCHEMA)
        except sqlite3.Error:
            self._fts = False  # prehistoric sqlite: vault works, keyword search won't
        self._migrate_to_v2()
        self._migrate_to_v3()
        self._migrate_to_v4()
        # idx_mem_ref lives here (not SCHEMA): SCHEMA must apply cleanly to
        # pre-migration v1 tables that have no content_ref column yet.
        try:
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_ref ON memories(content_ref)")
            self.conn.commit()
        except sqlite3.Error:
            log.warning("content_ref index was not created", exc_info=True)
        if self._fts:
            self._backfill_fts()
        if create:
            if self._vec:
                try:
                    self.conn.execute(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS mem_vec USING vec0(embedding float[{dims}])"
                    )
                except sqlite3.Error:
                    self._vec = False
            self._set_meta("embed_model", embed_name)
            self._set_meta("dims", str(dims))
            self._set_meta("schema", "4")
            self._set_meta("doc_threshold",
                           str(DEFAULT_DOC_THRESHOLD if doc_threshold is None else doc_threshold))
            self.conn.commit()
        else:
            got_model = self._get_meta("embed_model")
            got_dims = self._get_meta("dims")
            if got_model != embed_name or got_dims != str(dims):
                raise SpaceMismatchError(
                    f"vault is {got_model}/{got_dims}d but embedder is {embed_name}/{dims}d — "
                    "vectors from different spaces are never compared. "
                    "Use the original embedder or `cairn init` a new vault."
                )
        try:
            self._doc_threshold = int(self._get_meta("doc_threshold") or DEFAULT_DOC_THRESHOLD)
        except ValueError:
            self._doc_threshold = DEFAULT_DOC_THRESHOLD
        self._txn = False
        self._vec_ok = self._vec_index_ok()

    @property
    def vault_dir(self) -> Path:
        return self.db_path.parent

    @property
    def vault_identity(self) -> VaultIdentity:
        return self._vault_identity

    def reopen(self) -> Vault:
        return Vault(self.db_path, self._embed_name, self._dims)

    @property
    def doc_threshold(self) -> int:
        return self._doc_threshold

    # -- content-addressed docs -------------------------------------------
    # Memories over the threshold spill full text to docs/<aa>/<bb>/<hash>.md
    # (original bytes; filename is content_hash of the normalized text).
    # Rows keep content=NULL + content_ref=hash; small memories stay inline.
    def doc_path(self, content_hash: str) -> Path:
        h = content_hash.split(":", 1)[-1]  # bare hex: no scheme, no colons on disk
        return self.docs_root / h[:2] / h[2:4] / (h + ".md")

    def write_doc(self, content_hash: str, content: str) -> None:
        p = self.doc_path(content_hash)
        if p.exists():
            return  # dedupe: same hash, same bytes
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def read_content(self, row) -> str:
        """Resolve a memory row to full text (inline or docs/). Verifies hash."""
        if row["content"] is not None:
            return row["content"]
        ref = row["content_ref"]
        p = self.doc_path(ref)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            raise ContentIntegrityError(
                f"document missing for {row['key']}: expected {p} — "
                "restore it from a peer pack (`cairn export`/`import`) or purge the memory")
        from .models import content_digest  # local import: models has no store dep

        if f"sha256:{content_digest(text)}" != ref:  # content_hash carries the scheme prefix
            raise ContentIntegrityError(
                f"document for {row['key']} fails its hash check ({p}) — "
                "file changed under us; restore from a peer pack or purge the memory")
        return text

    def content_refcount(self, content_hash: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE content_hash=?", (content_hash,)).fetchone()["c"]

    def delete_doc_if_orphan(self, content_hash: str) -> bool:
        """Unlink the doc file when no row references its hash. Returns True if removed."""
        if self.content_refcount(content_hash):
            return False
        try:
            self.doc_path(content_hash).unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def sweep_orphan_docs(self) -> int:
        """Remove doc files with zero referencing rows (crash orphans, etc).

        Filenames are the bare hex. Rows store ``sha256:<hex>``. Compare stems
        to the hex half, or every live file looks orphaned.
        """
        removed = 0
        if not self.docs_root.is_dir():
            return 0
        live = {
            (r["content_hash"] or "").split(":", 1)[-1]
            for r in self.conn.execute(
                "SELECT DISTINCT content_hash FROM memories WHERE content_ref IS NOT NULL"
            )
        }
        for p in sorted(self.docs_root.rglob("*.md")):
            if p.stem in live:
                continue
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def doc_stats(self) -> dict:
        files, nbytes = 0, 0
        if self.docs_root.is_dir():
            for p in self.docs_root.rglob("*.md"):
                try:
                    files += 1
                    nbytes += p.stat().st_size
                except OSError:
                    pass
        return {"files": files, "bytes": nbytes}

    # -- meta -------------------------------------------------------------
    def _get_meta(self, k: str) -> str | None:
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None

    def _backfill_fts(self) -> None:
        """One-time migration: pre-FTS vaults get their content indexed on open."""
        try:
            n_mem = self.conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
            if not n_mem:
                return
            n_fts = self.conn.execute("SELECT COUNT(*) c FROM mem_fts").fetchone()["c"]
            if n_fts < n_mem:
                self.conn.execute(
                    "INSERT INTO mem_fts(rowid, key, content) "
                    "SELECT rowid, key, content FROM memories "
                    "WHERE content IS NOT NULL AND rowid NOT IN (SELECT rowid FROM mem_fts)"
                )
                # external rows (content spilled to docs/) indexed from files
                missing = self.conn.execute(
                    "SELECT rowid, key, content_ref FROM memories "
                    "WHERE content IS NULL AND rowid NOT IN (SELECT rowid FROM mem_fts)"
                ).fetchall()
                for r in missing:
                    try:
                        text = self.doc_path(r["content_ref"]).read_text(encoding="utf-8")
                    except OSError:
                        continue
                    self.conn.execute(
                        "INSERT INTO mem_fts(rowid, key, content) VALUES(?, ?, ?)",
                        (r["rowid"], r["key"], text),
                    )
                self.conn.commit()
        except sqlite3.Error:
            log.warning("FTS backfill skipped; writes still succeed", exc_info=True)

    def _migrate_to_v2(self) -> None:
        """Schema 1 -> 2: nullable content + content_ref (+CHECK), FTS triggers
        with WHEN guards. Rowids preserved so mem_vec/mem_fts stay valid.

        Single executescript (no partial-transaction games); the pre-migration
        backup is the safety net and orphan memories_new is cleaned on failure.
        Loud on error — a half-migrated vault must never look healthy.
        """
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(memories)").fetchall()]
        if cols and "content_ref" not in cols:  # existing v1 table; fresh DBs already have v2
                backup = self.db_path.parent / (self.db_path.name + ".pre2.bak")
                try:
                    import sqlite3 as _sq

                    src = _sq.connect(str(self.db_path))
                    try:
                        dst = _sq.connect(str(backup))
                        try:
                            src.backup(dst)
                        finally:
                            dst.close()
                    finally:
                        src.close()
                except (OSError, sqlite3.Error) as e:
                    raise RuntimeError(f"pre-migration backup failed, refusing to migrate: {e}") from e
                try:
                    self.conn.executescript("""
                        CREATE TABLE memories_new(
                          rowid INTEGER PRIMARY KEY,
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
                          created_at INTEGER NOT NULL,
                          updated_at INTEGER NOT NULL,
                          expires_at INTEGER,
                          archived_at INTEGER,
                          supersedes TEXT,
                          parent_key TEXT,
                          provenance TEXT,
                          confidence REAL,
                          content_hash TEXT NOT NULL,
                          embedding BLOB NOT NULL,
                          CHECK ((content IS NULL) != (content_ref IS NULL))
                        );
                        INSERT INTO memories_new(rowid, key, canonical_id, content, content_ref,
                          content_summary, memory_type, status, origin, task_id, agent_id, team_id,
                          version, created_at, updated_at, expires_at, archived_at, supersedes,
                          parent_key, provenance, confidence, content_hash, embedding)
                        SELECT rowid, key, canonical_id, content, NULL,
                          content_summary, memory_type, status, origin, task_id, agent_id, team_id,
                          version, created_at, updated_at, expires_at, archived_at, supersedes,
                          parent_key, provenance, confidence, content_hash, embedding FROM memories;
                        DROP TABLE memories;
                        ALTER TABLE memories_new RENAME TO memories;
                        CREATE INDEX IF NOT EXISTS idx_mem_task ON memories(task_id, created_at);
                        CREATE INDEX IF NOT EXISTS idx_mem_canon ON memories(canonical_id);
                        CREATE INDEX IF NOT EXISTS idx_mem_hash ON memories(content_hash);
                        CREATE INDEX IF NOT EXISTS idx_mem_status ON memories(status);
                        CREATE INDEX IF NOT EXISTS idx_mem_ref ON memories(content_ref);
                    """)
                except sqlite3.Error:
                    try:
                        self.conn.execute("DROP TABLE IF EXISTS memories_new")
                        self.conn.commit()
                    except sqlite3.Error:
                        log.warning("could not drop memories_new after a failed migration", exc_info=True)
                    raise
        if self._fts:
            trigs = {r["name"] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}
            if not {"mem_fts_ai", "mem_fts_ad", "mem_fts_au"} <= trigs:
                self.conn.execute("DROP TRIGGER IF EXISTS mem_fts_ai")
                self.conn.execute("DROP TRIGGER IF EXISTS mem_fts_ad")
                self.conn.execute("DROP TRIGGER IF EXISTS mem_fts_au")
                self.conn.executescript(FTS_SCHEMA)
        self._set_meta("schema", "2")
        if self._get_meta("doc_threshold") is None:
            self._set_meta("doc_threshold", str(DEFAULT_DOC_THRESHOLD))
        self.conn.commit()

    def _migrate_to_v3(self) -> None:
        """Add durable sync state, event, cursor, and conflict tables."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(memories)")}
        for name, declaration in (
            ("state_revision", "INTEGER NOT NULL DEFAULT 1"),
            ("state_origin", "TEXT NOT NULL DEFAULT ''"),
            ("state_event_id", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {declaration}")
        token_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(server_tokens)")}
        if "curator" not in token_cols:
            self.conn.execute("ALTER TABLE server_tokens ADD COLUMN curator INTEGER NOT NULL DEFAULT 0")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sync_events(
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id TEXT UNIQUE NOT NULL,
              event_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_tombstones(
              key TEXT PRIMARY KEY, event_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_cursors(
              peer TEXT NOT NULL, direction TEXT NOT NULL, cursor INTEGER NOT NULL,
              updated_at INTEGER NOT NULL, PRIMARY KEY(peer, direction)
            );
            CREATE TABLE IF NOT EXISTS sync_conflicts(
              base_key TEXT PRIMARY KEY, competitor_keys TEXT NOT NULL,
              detected_at INTEGER NOT NULL, resolved_at INTEGER, winner_key TEXT,
              resolved_by TEXT
            );
        """)
        if not self._get_meta("sync_origin_id"):
            self._set_meta("sync_origin_id", uuid.uuid4().hex)
        if self._get_meta("sync_origin_seq") is None:
            self._set_meta("sync_origin_seq", "0")
        origin = self._get_meta("sync_origin_id")
        pending = self.conn.execute(
            "SELECT * FROM memories WHERE state_origin='' OR state_event_id='' ORDER BY rowid"
        ).fetchall()
        for row in pending:
            event = self._new_sync_event("snapshot", row["key"], int(row["state_revision"]),
                                         int(row["updated_at"]), origin)
            snapshot = {field: row[field] for field in MEMORY_FIELDS}
            snapshot["content"] = self.read_content(row)
            snapshot["embedding"] = base64.b64encode(bytes(row["embedding"])).decode("ascii")
            snapshot["state_origin"] = origin
            snapshot["state_event_id"] = event["event_id"]
            event["snapshot"] = snapshot
            self.conn.execute(
                "UPDATE memories SET state_origin=?, state_event_id=? WHERE key=?",
                (origin, event["event_id"], row["key"]),
            )
            self._append_sync_event(event)
        schema = self._get_meta("schema")
        if schema is None or int(schema) < 3:
            self._set_meta("schema", "3")
        self.conn.commit()

    def _migrate_to_v4(self) -> None:
        """Add stable vault identity and scope sync cursors by vault and token."""
        schema = int(self._get_meta("schema") or 0)
        if schema > 4:
            raise RuntimeError(f"SQLite vault schema {schema} is newer than this Cairn version")
        if schema == 4:
            self._vault_identity = load_vault_identity(self._get_meta)
            columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(sync_cursors)")}
            if not {"vault_id", "token_id"} <= columns:
                raise RuntimeError("vault cursor schema is incomplete; refusing to use this vault")
            return

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._vault_identity = ensure_vault_identity(
                self._get_meta, self._set_meta, self.vault_dir,
            )
            columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(sync_cursors)")}
            if not {"vault_id", "token_id"} <= columns:
                rows = self.conn.execute(
                    "SELECT peer, direction, cursor, updated_at FROM sync_cursors"
                ).fetchall()
                self.conn.execute("DROP TABLE sync_cursors")
                self.conn.execute("""
                    CREATE TABLE sync_cursors(
                      vault_id TEXT NOT NULL, peer TEXT NOT NULL, token_id TEXT NOT NULL,
                      direction TEXT NOT NULL, cursor INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                      PRIMARY KEY(vault_id, peer, token_id, direction)
                    )
                """)
                for row in rows:
                    peer, token_id = split_legacy_cursor_peer(row["peer"])
                    self.conn.execute("""
                        INSERT INTO sync_cursors(
                          vault_id, peer, token_id, direction, cursor, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        ON CONFLICT(vault_id, peer, token_id, direction) DO UPDATE SET
                          cursor=MAX(sync_cursors.cursor, excluded.cursor),
                          updated_at=MAX(sync_cursors.updated_at, excluded.updated_at)
                    """, (self._vault_identity.vault_id, peer, token_id,
                          row["direction"], row["cursor"], row["updated_at"]))
            self._set_meta("schema", "4")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _new_sync_event(self, kind: str, key: str, revision: int, updated_at: int,
                        origin: str | None = None) -> dict:
        origin = origin or self._get_meta("sync_origin_id")
        seq = int(self._get_meta("sync_origin_seq") or "0") + 1
        self._set_meta("sync_origin_seq", str(seq))
        return {
            "event_id": f"{origin}:{seq}", "origin_id": origin, "origin_seq": seq,
            "kind": kind, "key": key, "state_revision": revision,
            "updated_at": updated_at, "state_origin": origin,
            "state_event_id": f"{origin}:{seq}",
        }

    def _append_sync_event(self, event: dict) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO sync_events(event_id, event_json) VALUES(?, ?)",
            (event["event_id"], json.dumps(event, separators=(",", ":"), default=str)),
        )

    def _set_meta(self, k: str, v: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, v))

    def _vec_table(self) -> bool:
        if not self._vec:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mem_vec'"
        ).fetchone()
        return row is not None

    def _vec_index_ok(self) -> bool:
        """True when mem_vec has one row per memory. A hole means brute force."""
        if not self._vec_table():
            return False
        try:
            n_mem = self.conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
            n_vec = self.conn.execute("SELECT COUNT(*) c FROM mem_vec").fetchone()["c"]
        except sqlite3.Error:
            return False
        return n_mem == n_vec

    def vec_status(self) -> dict:
        rows = None
        if self._vec_table():
            try:
                rows = self.conn.execute("SELECT COUNT(*) c FROM mem_vec").fetchone()["c"]
            except sqlite3.Error:
                rows = None
        return {
            "vec_extension": self._vec,
            "vec_rows": rows,
            "vec_in_sync": self._vec_index_ok(),
        }

    def rebuild_vec(self) -> dict:
        """Rebuild mem_vec from stored blobs. Searches use brute force until this matches."""
        if not self._vec:
            raise RuntimeError("sqlite-vec is not loaded")
        dims = int(self._get_meta("dims") or 0)
        if dims <= 0:
            raise RuntimeError("vault has no dims meta; refusing to rebuild mem_vec")
        import sqlite_vec as _sv

        rows = self.conn.execute("SELECT rowid, embedding FROM memories").fetchall()
        try:
            self.conn.execute("DROP TABLE IF EXISTS mem_vec")
            self.conn.execute(
                f"CREATE VIRTUAL TABLE mem_vec USING vec0(embedding float[{dims}])"
            )
            for r in rows:
                vec = np.frombuffer(r["embedding"], dtype=np.float32)
                if vec.size != dims:
                    raise RuntimeError(
                        f"row {r['rowid']} embedding is {vec.size}d, vault is {dims}d"
                    )
                self.conn.execute(
                    "INSERT INTO mem_vec(rowid, embedding) VALUES(?, ?)",
                    (r["rowid"], _sv.serialize_float32(vec.tolist())),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            self._vec_ok = False
            raise
        self._vec_ok = True
        return {"rebuilt": len(rows)}

    @contextmanager
    def transaction(self):
        """One commit for the block. Nested calls join the open transaction."""
        if self._txn:
            yield
            return
        self._txn = True
        try:
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._txn = False

    # -- writes ------------------------------------------------------------
    def insert(self, rec: dict, vector: np.ndarray) -> int:
        # rec["content"] is ALWAYS full text at this boundary (client, import);
        # spill to docs/ here so every write path shares one policy.
        rec = dict(rec)
        local_event = None
        if not rec.get("state_event_id"):
            origin = self._get_meta("sync_origin_id")
            revision = int(rec.get("state_revision", 1))
            local_event = self._new_sync_event(
                "snapshot", rec["key"], revision, int(rec["updated_at"]), origin,
            )
            rec.update({"state_revision": revision, "state_origin": origin,
                        "state_event_id": local_event["event_id"]})
        text = rec["content"]
        digest = rec.get("content_hash")
        if digest is None:
            from .models import content_digest

            digest = content_digest(text)
        if len(text.encode("utf-8")) > self._doc_threshold:
            self.write_doc(digest, text)
            content, ref = None, digest
        else:
            content, ref = text, None
        blob = np.asarray(vector, dtype=np.float32).tobytes()
        vals = []
        for c in COLUMNS:
            if c == "rowid":
                continue
            if c == "embedding":
                vals.append(blob)
            elif c == "content":
                vals.append(content)
            elif c == "content_ref":
                vals.append(ref)
            elif c == "content_hash":
                vals.append(digest)
            else:
                vals.append(rec[c])
        cols = [c for c in COLUMNS if c != "rowid"]
        try:
            cur = self.conn.execute(
                f"INSERT INTO memories({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
                vals,
            )
            rowid = cur.lastrowid
            if ref is not None and self._fts:
                # trigger skips NULL-content rows (WHEN guard) — index explicitly
                try:
                    self.conn.execute(
                        "INSERT INTO mem_fts(rowid, key, content) VALUES(?, ?, ?)",
                        (rowid, rec["key"], text),
                    )
                except sqlite3.Error:
                    pass
            if self._vec_ok:
                import sqlite_vec as _sv

                self.conn.execute(
                    "INSERT INTO mem_vec(rowid, embedding) VALUES(?, ?)",
                    (rowid, _sv.serialize_float32(np.asarray(vector, dtype=np.float32).tolist())),
                )
            if local_event is not None:
                snapshot = {field: rec.get(field) for field in MEMORY_FIELDS}
                snapshot["content"] = text
                snapshot["embedding"] = base64.b64encode(blob).decode("ascii")
                local_event["snapshot"] = snapshot
                self._append_sync_event(local_event)
            if not self._txn:
                self.conn.commit()
            return rowid
        except Exception:
            if not self._txn:
                self.conn.rollback()
            raise

    def set_status(self, key: str, status: str, now: int, archived_at: int | None = None,
                   event_metadata: dict | None = None) -> int:
        row = self.conn.execute("SELECT * FROM memories WHERE key=?", (key,)).fetchone()
        if row is None:
            return 0
        revision = int(row["state_revision"]) + 1
        event = self._new_sync_event("state", key, revision, now)
        event.update({"status": status, "archived_at": archived_at})
        event.update(event_metadata or {})
        if event.get("conflict_resolution") is not None:
            self._validate_resolution_event(event, event["conflict_resolution"])
        cur = self.conn.execute(
            "UPDATE memories SET status=?, updated_at=?, archived_at=?, state_revision=?, "
            "state_origin=?, state_event_id=? WHERE key=?",
            (status, now, archived_at, revision, event["state_origin"], event["event_id"], key),
        )
        self._append_sync_event(event)
        self._record_resolution_event(event)
        if not self._txn:
            self.conn.commit()
        return cur.rowcount

    def delete_by_keys(self, keys: list[str], reason: str = "deleted") -> int:
        if not keys:
            return 0
        try:
            rows = self.conn.execute(
                f"SELECT * FROM memories WHERE key IN ({','.join('?' for _ in keys)})",
                keys,
            ).fetchall()
            rowids = [r["rowid"] for r in rows]
            hashes = [r["content_hash"] for r in rows]
            for row in rows:
                event = self._new_sync_event(
                    "tombstone", row["key"], int(row["state_revision"]) + 1,
                    int(time.time()),
                )
                event.update({"canonical_id": row["canonical_id"],
                              "agent_id": row["agent_id"], "reason": reason})
                self._append_sync_event(event)
                self._remember_tombstone(event)
            self.conn.execute(
                f"DELETE FROM memories WHERE key IN ({','.join('?' for _ in keys)})", keys
            )
            if self._vec_ok and rowids:
                self.conn.execute(
                    f"DELETE FROM mem_vec WHERE rowid IN ({','.join('?' for _ in rowids)})",
                    rowids,
                )
            if not self._txn:
                self.conn.commit()
        except Exception:
            if not self._txn:
                self.conn.rollback()
            raise
        if not self._txn:
            for h in dict.fromkeys(hashes):  # refcounted: file goes only with its last row
                try:
                    self.delete_doc_if_orphan(h)
                except OSError:
                    pass
        return len(rowids)

    def delete_by_canonical(self, canonical_id: str, reason: str = "purged") -> int:
        keys = [
            r["key"]
            for r in self.conn.execute(
                "SELECT key FROM memories WHERE canonical_id=?", (canonical_id,)
            ).fetchall()
        ]
        return self.delete_by_keys(keys, reason)

    # -- reads --------------------------------------------------------------
    def get(self, key: str) -> sqlite3.Row | None:
        cols = ", ".join(READ_COLUMNS)
        return self.conn.execute(
            f"SELECT {cols} FROM memories WHERE key=?", (key,)
        ).fetchone()

    def by_hash(self, content_hash: str, task_id: str, status: str = "active") -> list[sqlite3.Row]:
        cols = ", ".join(READ_COLUMNS)
        return self.conn.execute(
            f"SELECT {cols} FROM memories WHERE content_hash=? AND task_id=? AND status=?",
            (content_hash, task_id, status),
        ).fetchall()

    def create_server_token(self, token_id: str, token_hash: str, agent_id: str,
                            created_at: int, curator: bool = False) -> None:
        self.conn.execute(
            "INSERT INTO server_tokens(token_id, token_hash, agent_id, curator, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (token_id, token_hash, agent_id, int(curator), created_at),
        )

    def get_server_token(self, token_hash: str) -> dict | None:
        row = self.conn.execute(
            "SELECT token_id, agent_id, curator, created_at FROM server_tokens WHERE token_hash=?",
            (token_hash,),
        ).fetchone()
        return ({**dict(row), "curator": bool(row["curator"])} if row else None)

    def list_server_tokens(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT token_id, agent_id, curator, created_at FROM server_tokens "
            "ORDER BY created_at, token_id"
        ).fetchall()
        return [{**dict(row), "curator": bool(row["curator"])} for row in rows]

    def delete_server_token(self, token_id: str) -> int:
        cur = self.conn.execute("DELETE FROM server_tokens WHERE token_id=?", (token_id,))
        return cur.rowcount

    def get_agent_ids(self, keys: list[str]) -> dict[str, str]:
        agents: dict[str, str] = {}
        for start in range(0, len(keys), 500):
            batch = keys[start:start + 500]
            marks = ",".join("?" for _ in batch)
            rows = self.conn.execute(
                f"SELECT key, agent_id FROM memories WHERE key IN ({marks})", batch
            ).fetchall()
            agents.update({row["key"]: row["agent_id"] for row in rows})
        return agents

    # -- durable sync -----------------------------------------------------
    def sync_origin_id(self) -> str:
        return self._get_meta("sync_origin_id") or ""

    def export_sync_events(self, after: int = 0, limit: int = 100_000) -> dict:
        rows = self.conn.execute(
            "SELECT seq, event_json FROM sync_events WHERE seq>? ORDER BY seq LIMIT ?",
            (after, limit + 1),
        ).fetchall()
        rows = rows[:limit]
        events = [{**json.loads(row["event_json"]), "feed_seq": row["seq"]} for row in rows]
        cursor = rows[-1]["seq"] if rows else after
        return {"pack": "cairn-sync-2", "after": after, "cursor": cursor,
                "events": events}

    def has_sync_event(self, event_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sync_events WHERE event_id=?", (event_id,),
        ).fetchone() is not None

    def has_sync_tombstone(self, key: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sync_tombstones WHERE key=?", (key,),
        ).fetchone() is not None

    def _remember_tombstone(self, event: dict) -> None:
        self.conn.execute(
            "INSERT INTO sync_tombstones(key, event_json) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET event_json=excluded.event_json",
            (event["key"], json.dumps(event, separators=(",", ":"), default=str)),
        )

    def apply_sync_event(self, event: dict) -> str:
        if not self._txn:
            with self.transaction():
                return self.apply_sync_event(event)
        if not isinstance(event, dict) or not isinstance(event.get("event_id"), str):
            raise ValueError("invalid sync event: missing event_id")
        if self.conn.execute("SELECT 1 FROM sync_events WHERE event_id=?", (event["event_id"],)).fetchone():
            return "skipped"
        kind = event.get("kind")
        key = event.get("key")
        if kind not in {"snapshot", "state", "tombstone"} or not isinstance(key, str):
            raise ValueError("invalid sync event kind or key")
        existing = self.conn.execute("SELECT * FROM memories WHERE key=?", (key,)).fetchone()
        incoming_order = _sync_event_order(event)
        current_order = _sync_row_order(existing) if existing else None
        resolution = event.get("conflict_resolution")
        if resolution is not None and not (kind == "state" and existing is None
                                            and self.has_sync_tombstone(key)):
            self._validate_resolution_event(event, resolution)
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
                vector = np.frombuffer(base64.b64decode(raw), dtype=np.float32).copy()
                rec = {field: snapshot.get(field) for field in MEMORY_FIELDS}
                rec["content"] = snapshot.get("content")
                self.insert(rec, vector)
                changed = True
            elif incoming_order > current_order:
                self.conn.execute(
                    "UPDATE memories SET status=?, updated_at=?, archived_at=?, expires_at=?, "
                    "state_revision=?, state_origin=?, state_event_id=? WHERE key=?",
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
                self.conn.execute(
                    "UPDATE memories SET status=?, updated_at=?, archived_at=?, state_revision=?, "
                    "state_origin=?, state_event_id=? WHERE key=?",
                    (event.get("status"), event.get("updated_at"), event.get("archived_at"),
                     event.get("state_revision"), event.get("state_origin", event.get("origin_id")),
                     event.get("state_event_id", event["event_id"]), key),
                )
                changed = True
        else:
            self._remember_tombstone(event)
            if existing is not None:
                rowid, content_hash = existing["rowid"], existing["content_hash"]
                self.conn.execute("DELETE FROM memories WHERE key=?", (key,))
                if self._vec_ok:
                    self.conn.execute("DELETE FROM mem_vec WHERE rowid=?", (rowid,))
                self.delete_doc_if_orphan(content_hash)
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
        winner_key = resolution.get("winner_key")
        resolved_by = resolution.get("resolved_by") or "sync"
        if not isinstance(base_key, str) or not isinstance(competitors, list):
            return
        self.conn.execute(
            "INSERT INTO sync_conflicts(base_key, competitor_keys, detected_at, resolved_at, "
            "winner_key, resolved_by) VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(base_key) DO UPDATE SET "
            "competitor_keys=excluded.competitor_keys, resolved_at=excluded.resolved_at, "
            "winner_key=excluded.winner_key, resolved_by=excluded.resolved_by",
            (base_key, json.dumps(competitors), event.get("updated_at", 0),
             event.get("updated_at", 0), winner_key, resolved_by),
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
                or event.get("key") not in competitors
                or winner_key not in competitors or event.get("key") == winner_key):
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

    def get_sync_cursor(self, peer: str, direction: str, token_id: str = "") -> int:
        row = self.conn.execute(
            "SELECT cursor FROM sync_cursors WHERE vault_id=? AND peer=? AND token_id=? AND direction=?",
            (self.vault_identity.vault_id, peer, token_id, direction),
        ).fetchone()
        return int(row["cursor"]) if row else 0

    def list_sync_cursors(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT vault_id, peer, token_id, direction, cursor, updated_at FROM sync_cursors "
            "ORDER BY vault_id, peer, token_id, direction"
        ).fetchall()]

    def set_sync_cursor(self, peer: str, direction: str, cursor: int, now: int,
                        token_id: str = "") -> None:
        if direction not in {"push", "pull"} or cursor < 0:
            raise ValueError("invalid sync cursor")
        self.conn.execute(
            "INSERT INTO sync_cursors(vault_id, peer, token_id, direction, cursor, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(vault_id, peer, token_id, direction) DO UPDATE SET "
            "cursor=MAX(sync_cursors.cursor, excluded.cursor), "
            "updated_at=excluded.updated_at",
            (self.vault_identity.vault_id, peer, token_id, direction, cursor, now),
        )
        if not self._txn:
            self.conn.commit()

    def list_competing_corrections(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT supersedes AS base_key, GROUP_CONCAT(key) AS keys "
            "FROM memories WHERE status='active' AND supersedes IS NOT NULL "
            "GROUP BY supersedes HAVING COUNT(*) > 1 ORDER BY supersedes"
        ).fetchall()
        return [{"base_key": row["base_key"], "competitor_keys": sorted(row["keys"].split(","))}
                for row in rows]

    def record_competing_corrections(self, now: int) -> list[dict]:
        conflicts = self.list_competing_corrections()
        for item in conflicts:
            self.conn.execute(
                "INSERT INTO sync_conflicts(base_key, competitor_keys, detected_at) VALUES(?, ?, ?) "
                "ON CONFLICT(base_key) DO UPDATE SET competitor_keys=excluded.competitor_keys, "
                "detected_at=CASE WHEN sync_conflicts.resolved_at IS NULL THEN "
                "sync_conflicts.detected_at ELSE excluded.detected_at END, resolved_at=NULL, "
                "winner_key=NULL, resolved_by=NULL",
                (item["base_key"], json.dumps(item["competitor_keys"]), now),
            )
        unresolved = self.conn.execute(
            "SELECT base_key, competitor_keys FROM sync_conflicts WHERE resolved_at IS NULL"
        ).fetchall()
        for row in unresolved:
            keys = json.loads(row["competitor_keys"])
            if not keys:
                continue
            active = self.conn.execute(
                f"SELECT key FROM memories WHERE status='active' AND key IN "
                f"({','.join('?' for _ in keys)})", keys,
            ).fetchall()
            if len(active) == 1:
                self.conn.execute(
                    "UPDATE sync_conflicts SET resolved_at=?, winner_key=?, resolved_by='sync' "
                    "WHERE base_key=? AND resolved_at IS NULL",
                    (now, active[0]["key"], row["base_key"]),
                )
        if not self._txn:
            self.conn.commit()
        return conflicts

    def list_sync_conflicts(self, include_resolved: bool = False) -> list[dict]:
        clause = "" if include_resolved else " WHERE resolved_at IS NULL"
        rows = self.conn.execute(
            "SELECT * FROM sync_conflicts" + clause + " ORDER BY detected_at, base_key"
        ).fetchall()
        return [{**dict(row), "competitor_keys": json.loads(row["competitor_keys"]),
                 "resolved": row["resolved_at"] is not None} for row in rows]

    def resolve_sync_conflict(self, base_key: str, winner_key: str,
                              resolved_by: str, now: int) -> None:
        row = self.conn.execute(
            "SELECT competitor_keys FROM sync_conflicts WHERE base_key=?", (base_key,),
        ).fetchone()
        if row is None:
            raise KeyError(f"no recorded competing correction for {base_key}")
        competitors = json.loads(row["competitor_keys"])
        if winner_key not in competitors:
            raise ValueError(f"winner must be one of the competing corrections: {competitors}")
        self.conn.execute(
            "UPDATE sync_conflicts SET resolved_at=?, winner_key=?, resolved_by=? WHERE base_key=?",
            (now, winner_key, resolved_by, base_key),
        )
        if not self._txn:
            self.conn.commit()

    def scan(self, where: str = "", args: tuple = (), limit: int = 100,
             with_embedding: bool = False) -> list[sqlite3.Row]:
        cols = "*" if with_embedding else ", ".join(READ_COLUMNS)
        q = f"SELECT {cols} FROM memories"
        if where:
            q += f" WHERE {where}"
        q += " ORDER BY created_at ASC LIMIT ?"
        return self.conn.execute(q, (*args, limit)).fetchall()

    def find(self, query: MemoryQuery, limit: int = 100,
             with_embedding: bool = False) -> list[sqlite3.Row]:
        # eq columns are checked against FILTER_COLUMNS by MemoryQuery
        clauses = [f"{col}=?" for col in query.eq]
        args = list(query.eq.values())
        for field, sql in QUERY_BOUNDS:
            value = getattr(query, field)
            if value is not None:
                clauses.append(sql)
                args.append(value)
        return self.scan(" AND ".join(clauses), tuple(args), limit, with_embedding)

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]

    def count_by_status(self) -> dict[str, int]:
        return {
            r["status"]: r["c"]
            for r in self.conn.execute(
                "SELECT status, COUNT(*) c FROM memories GROUP BY status").fetchall()
        }

    @staticmethod
    def sanitize_fts(text: str) -> str | None:
        """Quote each whitespace-separated term so user input is always a safe
        literal AND query (no FTS operators, no syntax errors from `"*(` etc)."""
        terms = [t.replace('"', '""') for t in text.split() if t.strip('*"')]
        terms = [t for t in terms if t.strip('"')]
        if not terms:
            return None
        return " ".join(f'"{t}"' for t in terms)

    def fts_search(self, text: str, limit: int = 100, status: str = "active",
                   extra: dict | None = None) -> list[sqlite3.Row]:
        """BM25 keyword search over content. Returns memory rows best-first.

        extra maps exact-match columns (task_id, memory_type, team_id,
        agent_id, canonical_id) to values. Raises RuntimeError without FTS5.
        """
        if not self._fts:
            raise RuntimeError("this sqlite build has no FTS5 — keyword search unavailable")
        match = self.sanitize_fts(text)
        if match is None:
            return []
        clauses: list[str] = ["mem_fts MATCH ?", "m.status=?"]
        args: list = [match, status]
        for col in ("task_id", "canonical_id", "memory_type", "team_id", "agent_id"):
            if extra and extra.get(col) is not None:
                clauses.append(f"m.{col}=?")
                args.append(extra[col])
        args.append(limit)
        cols = ", ".join(f"m.{c}" for c in READ_COLUMNS)
        return self.conn.execute(
            f"SELECT {cols}, f.rank AS _rank FROM mem_fts AS f "
            "JOIN memories AS m ON m.rowid = f.rowid "
            f"WHERE {' AND '.join(clauses)} ORDER BY _rank LIMIT ?",
            args,
        ).fetchall()

    def all_vectors(self) -> list[tuple[int, bytes]]:
        return [
            (r["rowid"], r["embedding"])
            for r in self.conn.execute("SELECT rowid, embedding FROM memories").fetchall()
        ]

    def _filter_sql(self, status: str, filters: dict | None, now: int | None) -> tuple[str, tuple]:
        clauses = ["status=?"]
        args: list = [status]
        for col in ("task_id", "memory_type", "team_id", "agent_id"):
            if filters and filters.get(col) is not None:
                clauses.append(f"{col}=?")
                args.append(filters[col])
        if now is not None:
            clauses.append("(expires_at IS NULL OR expires_at>?)")
            args.append(now)
        return " AND ".join(clauses), tuple(args)

    def _score_rows(self, q: np.ndarray, rows) -> list[tuple[int, float]]:
        scored = [
            (r["rowid"], _cos_dist(q, np.frombuffer(r["embedding"], dtype=np.float32)))
            for r in rows
        ]
        scored.sort(key=lambda t: t[1])
        return scored

    def _hydrate(self, scored: list[tuple[int, float]], k: int) -> list[tuple[sqlite3.Row, float]]:
        """Load full read columns for the closest k rowids only."""
        top = scored[:k]
        if not top:
            return []
        dist = {rowid: d for rowid, d in top}
        ids = list(dist)
        cols = ", ".join(READ_COLUMNS)
        rows = self.conn.execute(
            f"SELECT {cols} FROM memories WHERE rowid IN ({','.join('?' for _ in ids)})",
            ids,
        ).fetchall()
        out = [(r, dist[r["rowid"]]) for r in rows]
        out.sort(key=lambda t: t[1])
        return out

    # -- vector search -------------------------------------------------------
    def knn(self, query: np.ndarray, k: int, status: str = "active",
            filters: dict | None = None, now: int | None = None) -> list[tuple[sqlite3.Row, float]]:
        """Returns [(memory_row, cosine_distance)] ordered closest-first.

        Metadata filters and expiry apply before the top-k cut. sqlite-vec
        proposes candidates and widens the window until `k` survive; cosine
        is recomputed from stored blobs. A vec index whose row count does
        not match memories is ignored (brute force).
        """
        q = np.asarray(query, dtype=np.float32).ravel()
        where, fargs = self._filter_sql(status, filters, now)
        if self._vec_ok:
            try:
                import sqlite_vec as _sv

                qser = _sv.serialize_float32(q.tolist())
                total = self.count()
                limit = max(k * 8, 64)
                while True:
                    hits = self.conn.execute(
                        "SELECT rowid FROM mem_vec WHERE embedding MATCH ? "
                        "ORDER BY distance LIMIT ?",
                        (qser, limit),
                    ).fetchall()
                    if not hits:
                        return []
                    ids = [r["rowid"] for r in hits]
                    rows = self.conn.execute(
                        "SELECT rowid, embedding FROM memories "
                        f"WHERE rowid IN ({','.join('?' for _ in ids)}) AND {where}",
                        (*ids, *fargs),
                    ).fetchall()
                    scored = self._score_rows(q, rows)
                    if len(scored) >= k or len(hits) < limit or limit >= max(total, 1):
                        return self._hydrate(scored, k)
                    limit = min(max(total, 1), limit * 2)
            except sqlite3.Error:
                pass  # this call uses brute force; do not mark the index unused
        rows = self.conn.execute(
            f"SELECT rowid, embedding FROM memories WHERE {where}", fargs
        ).fetchall()
        return self._hydrate(self._score_rows(q, rows), k)

    def close(self) -> None:
        self.conn.close()


def _cos_dist(a: np.ndarray, b: np.ndarray) -> float:
    if b.size != a.size:
        return 1.0
    return float(1.0 - float(np.dot(a, b)))
