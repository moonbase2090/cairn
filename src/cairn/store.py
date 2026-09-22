"""SQLite-backed vault: metadata table + sqlite-vec ANN index (brute-force fallback).

One file per vault: `<dir>/vault.db`. The collection is tagged with its embed
model + dims at init; opening with a mismatched embedder is refused so vector
spaces are never mixed.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import numpy as np

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
    "created_at", "updated_at", "expires_at", "archived_at", "supersedes",
    "parent_key",     "provenance", "confidence", "content_hash", "embedding",
]
# Reads that do not score vectors skip the embedding blob.
READ_COLUMNS = [c for c in COLUMNS if c != "embedding"]

DEFAULT_DOC_THRESHOLD = 2048  # memories larger than this spill content to docs/


class SpaceMismatchError(ValueError):
    pass


class ContentIntegrityError(ValueError):
    """A docs/ file is missing or fails its hash check. Loud by design —
    integrity failures must never degrade to silent wrong answers."""


class Vault:
    def __init__(self, db_path: str | Path, embed_name: str, dims: int, create: bool = False,
                 doc_threshold: int | None = None):
        self.db_path = Path(db_path)
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
            except Exception:
                self._vec = False
        self.conn.executescript(SCHEMA)
        self._fts = True
        try:
            self.conn.executescript(FTS_SCHEMA)
        except Exception:
            self._fts = False  # prehistoric sqlite: vault works, keyword search won't
        self._migrate_to_v2()
        # idx_mem_ref lives here (not SCHEMA): SCHEMA must apply cleanly to
        # pre-migration v1 tables that have no content_ref column yet.
        try:
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_ref ON memories(content_ref)")
            self.conn.commit()
        except Exception:
            pass
        if self._fts:
            self._backfill_fts()
        self.docs_root = self.db_path.parent / "docs"
        if create:
            if self._vec:
                try:
                    self.conn.execute(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS mem_vec USING vec0(embedding float[{dims}])"
                    )
                except Exception:
                    self._vec = False
            self._set_meta("embed_model", embed_name)
            self._set_meta("dims", str(dims))
            self._set_meta("schema", "2")
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
        except Exception:
            pass  # FTS is a best-effort accelerator; writes still succeed

    def _migrate_to_v2(self) -> None:
        """Schema 1 -> 2: nullable content + content_ref (+CHECK), FTS triggers
        with WHEN guards. Rowids preserved so mem_vec/mem_fts stay valid.

        Single executescript (no partial-transaction games); the pre-migration
        backup is the safety net and orphan memories_new is cleaned on failure.
        Loud on error — a half-migrated vault must never look healthy.
        """
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(memories)").fetchall()]
        if "content_ref" not in cols:
            if cols:  # existing v1 table to rebuild (fresh DBs already have v2 shape)
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
                except Exception as e:
                    raise RuntimeError(f"pre-migration backup failed, refusing to migrate: {e}")
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
                except Exception:
                    try:
                        self.conn.execute("DROP TABLE IF EXISTS memories_new")
                        self.conn.commit()
                    except Exception:
                        pass
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
            if not self._txn:
                self.conn.commit()
            return rowid
        except Exception:
            if not self._txn:
                self.conn.rollback()
            raise

    def set_status(self, key: str, status: str, now: int, archived_at: int | None = None) -> int:
        cur = self.conn.execute(
            "UPDATE memories SET status=?, updated_at=?, archived_at=? WHERE key=?",
            (status, now, archived_at, key),
        )
        if not self._txn:
            self.conn.commit()
        return cur.rowcount

    def delete_by_keys(self, keys: list[str]) -> int:
        if not keys:
            return 0
        try:
            rows = self.conn.execute(
                f"SELECT rowid, content_hash FROM memories WHERE key IN ({','.join('?' for _ in keys)})",
                keys,
            ).fetchall()
            rowids = [r["rowid"] for r in rows]
            hashes = [r["content_hash"] for r in rows]
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

    def delete_by_canonical(self, canonical_id: str) -> int:
        keys = [
            r["key"]
            for r in self.conn.execute(
                "SELECT key FROM memories WHERE canonical_id=?", (canonical_id,)
            ).fetchall()
        ]
        return self.delete_by_keys(keys)

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

    def scan(self, where: str = "", args: tuple = (), limit: int = 100,
             with_embedding: bool = False) -> list[sqlite3.Row]:
        cols = "*" if with_embedding else ", ".join(READ_COLUMNS)
        q = f"SELECT {cols} FROM memories"
        if where:
            q += f" WHERE {where}"
        q += " ORDER BY created_at ASC LIMIT ?"
        return self.conn.execute(q, (*args, limit)).fetchall()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]

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
