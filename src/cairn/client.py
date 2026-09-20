"""CairnClient — the six memory verbs + lifecycle + git-syncable packs.

Cairn semantics: append-only versioning, exact-hash idempotency,
explicit supersession, read-collapse, origin trust tags, cite-the-key.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import numpy as np

from .embed import Embedder
from .models import (
    MemoryRecord,
    MemoryType,
    Origin,
    Status,
    StoreAction,
    StoreResult,
    build_key,
    content_digest,
    now_epoch,
)
from .store import Vault

NEAR_DUP_SIM = 0.95
OVERSAMPLE = 20


def _decode_embedding(raw) -> np.ndarray:
    """Accept cairn-export-1 binary (base64 float32) or legacy JSON float lists."""
    if isinstance(raw, str):
        return np.frombuffer(base64.b64decode(raw), dtype=np.float32).copy()
    return np.asarray(raw, dtype=np.float32)


def _row_to_record(row, content: str | None, similarity: float | None = None) -> MemoryRecord:
    return MemoryRecord(
        key=row["key"],
        canonical_id=row["canonical_id"],
        content=content if content is not None else row["content"],
        content_summary=row["content_summary"],
        memory_type=row["memory_type"],
        status=row["status"],
        origin=row["origin"],
        task_id=row["task_id"],
        agent_id=row["agent_id"],
        team_id=row["team_id"],
        version=row["version"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        confidence=row["confidence"],
        provenance=row["provenance"],
        content_hash=row["content_hash"],
        distance=(1.0 - similarity) if similarity is not None else None,
        similarity=similarity,
    )


class CairnClient:
    def __init__(self, vault: Vault, agent_id: str, embedder: Embedder, audit_path: Path | None = None):
        self.vault = vault
        self.agent_id = agent_id
        self.embedder = embedder
        self.audit_path = audit_path

    # -- internal ---------------------------------------------------------
    def _embed_one(self, text: str) -> np.ndarray:
        return np.asarray(self.embedder.embed([text])[0], dtype=np.float32)

    def _record(self, row, similarity: float | None = None) -> MemoryRecord:
        """Materialize a row, resolving docs/ content. Loud on corruption."""
        return _row_to_record(row, self.vault.read_content(row), similarity)

    def _audit(self, action: str, payload: dict) -> None:
        if not self.audit_path:
            return
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a") as f:
                f.write(json.dumps({"ts": now_epoch(), "agent": self.agent_id, "action": action, **payload}) + "\n")
        except OSError:
            pass

    # -- store -------------------------------------------------------------
    def store_memory(
        self,
        content: str,
        team_id: str,
        task_id: str,
        memory_type: str = "semantic",
        origin: str = "agent",
        supersedes_key: str | None = None,
        mode: str = "auto",
        confidence: float | None = None,
        provenance: str | None = None,
        expires_at: int | None = None,
        vector: np.ndarray | None = None,
    ) -> StoreResult:
        if memory_type not in {t.value for t in MemoryType}:
            raise ValueError(f"bad memory_type: {memory_type}")
        if origin not in {o.value for o in Origin}:
            raise ValueError(f"bad origin: {origin}")
        if content is None or not str(content).strip():
            raise ValueError("content must be non-empty (whitespace-only stores pollute retrieve)")
        now = now_epoch()
        digest = content_digest(content)

        if supersedes_key:
            target = self.vault.get(supersedes_key)
            if target is None:
                raise KeyError(f"supersedes target not found: {supersedes_key}")
            version = int(target["version"]) + 1
            canonical_id = target["canonical_id"]
            key = build_key(self.agent_id, task_id, digest, version)
            summary = (content[:200] or "").strip()
            vec = np.asarray(vector, dtype=np.float32) if vector is not None else self._embed_one(content)
            self.vault.insert(
                {"key": key, "canonical_id": canonical_id, "content": content,
                 "content_summary": summary, "memory_type": memory_type,
                 "status": Status.ACTIVE.value, "origin": origin, "task_id": task_id,
                 "agent_id": self.agent_id, "team_id": team_id, "version": version,
                 "created_at": now, "updated_at": now, "expires_at": expires_at,
                 "archived_at": None, "supersedes": supersedes_key, "parent_key": None,
                 "provenance": provenance, "confidence": confidence,
                 "content_hash": f"sha256:{digest}"},
                vec,
            )
            self.vault.set_status(supersedes_key, Status.SUPERSEDED.value, now)
            self._audit("store", {"key": key, "result": "superseded", "supersedes": supersedes_key})
            return StoreResult(key=key, version=version, action=StoreAction.SUPERSEDED, canonical_id=canonical_id)

        # exact-duplicate → idempotent no-op (task-scoped)
        dupes = self.vault.by_hash(f"sha256:{digest}", task_id)
        if dupes:
            row = dupes[0]
            return StoreResult(key=row["key"], version=row["version"],
                               action=StoreAction.UNCHANGED, canonical_id=row["canonical_id"])

        # near-duplicate screen
        vec = np.asarray(vector, dtype=np.float32) if vector is not None else self._embed_one(content)
        if mode == "auto":
            near = self._near_duplicates(vec, task_id, top=3)
            if near:
                return StoreResult(key=None, version=None,
                                   action=StoreAction.DUPLICATE_DETECTED,
                                   near_duplicates=near)

        version = 1
        canonical_id = f"{task_id}-{digest[:12]}"
        key = build_key(self.agent_id, task_id, digest, version)
        self.vault.insert(
            {"key": key, "canonical_id": canonical_id, "content": content,
             "content_summary": (content[:200] or "").strip(), "memory_type": memory_type,
             "status": Status.ACTIVE.value, "origin": origin, "task_id": task_id,
             "agent_id": self.agent_id, "team_id": team_id, "version": version,
             "created_at": now, "updated_at": now, "expires_at": expires_at,
             "archived_at": None, "supersedes": None, "parent_key": None,
             "provenance": provenance, "confidence": confidence,
             "content_hash": f"sha256:{digest}"},
            vec,
        )
        self._audit("store", {"key": key, "result": "created"})
        return StoreResult(key=key, version=version, action=StoreAction.CREATED, canonical_id=canonical_id)

    def _near_duplicates(self, vec: np.ndarray, task_id: str, top: int) -> list[MemoryRecord]:
        now = now_epoch()
        out: list[MemoryRecord] = []
        for row, dist in self.vault.knn(vec, OVERSAMPLE):
            if row["task_id"] != task_id:
                continue
            if row["expires_at"] is not None and row["expires_at"] <= now:
                continue
            sim = 1.0 - dist
            if sim >= NEAR_DUP_SIM:
                out.append(self._record(row, sim))
            if len(out) >= top:
                break
        return out

    # -- retrieve ------------------------------------------------------------
    def retrieve_memory(self, query: str, filters: dict | None = None, top_k: int = 5,
                        min_similarity: float | None = None) -> list[MemoryRecord]:
        """min_similarity drops everything below the floor (default None = legacy top-k)."""
        filters = filters or {}
        now = now_epoch()
        qvec = self._embed_one(query)
        best: dict[str, tuple[tuple[int, int], MemoryRecord]] = {}
        for row, dist in self.vault.knn(qvec, max(top_k * 4, OVERSAMPLE)):
            if row["expires_at"] is not None and row["expires_at"] <= now:
                continue
            if "task_id" in filters and row["task_id"] != filters["task_id"]:
                continue
            if "memory_type" in filters and row["memory_type"] != filters["memory_type"]:
                continue
            if "team_id" in filters and row["team_id"] != filters["team_id"]:
                continue
            sim = 1.0 - dist
            if min_similarity is not None and sim < min_similarity:
                continue
            rec = self._record(row, sim)
            k = row["canonical_id"]
            rank = (int(row["version"]), int(row["created_at"]))
            if k not in best or rank > best[k][0]:
                best[k] = (rank, rec)
        collapsed = sorted(best.values(), key=lambda t: (t[1].similarity or 0.0), reverse=True)
        return [rec for _, rec in collapsed[:top_k]]

    # -- exact verbs -----------------------------------------------------------
    def list_memories(self, filters: dict, limit: int = 100) -> list[MemoryRecord]:
        if filters.get("search"):
            # BM25 keyword lookup: active by default (explicit --status overrides),
            # expired dropped like retrieve, best match first. No version collapse —
            # list is an exact verb; use --canonical to walk a version group.
            status = filters.get("status") or "active"
            extra = {k: filters[k] for k in
                     ("task_id", "canonical_id", "memory_type", "team_id", "agent_id")
                     if filters.get(k) is not None}
            now = now_epoch()
            out = []
            for r in self.vault.fts_search(filters["search"], limit, status, extra or None):
                if r["expires_at"] is not None and r["expires_at"] <= now:
                    continue
                out.append(self._record(r))
            return out
        clauses, args = [], []
        for col in ("task_id", "canonical_id", "memory_type", "status", "team_id", "agent_id"):
            if col in filters and filters[col] is not None:
                clauses.append(f"{col}=?")
                args.append(filters[col])
        if not clauses:
            raise ValueError("list needs at least one filter (task_id, canonical_id, search, …)")
        rows = self.vault.scan(" AND ".join(clauses), tuple(args), limit)
        return [self._record(r) for r in rows]

    def get_memory(self, key: str) -> MemoryRecord | None:
        row = self.vault.get(key)
        return self._record(row) if row else None

    def archive_memory(self, key: str) -> dict:
        now = now_epoch()
        if self.vault.get(key) is None:
            raise KeyError(f"not found: {key}")
        self.vault.set_status(key, Status.ARCHIVED.value, now, archived_at=now)
        self._audit("archive", {"key": key})
        return {"key": key, "status": "archived"}

    def restore_memory(self, key: str) -> dict:
        row = self.vault.get(key)
        if row is None:
            raise KeyError(f"not found: {key}")
        if row["status"] == Status.ACTIVE.value:
            return {"key": key, "status": "active", "note": "already active"}
        now = now_epoch()
        retired: list[str] = []
        if row["status"] == Status.SUPERSEDED.value:
            # undoing a correction: the active rival(s) that replaced this memory
            # were the mistake — archive them so exactly one version stays active
            rivals = self.vault.scan(
                "canonical_id=? AND status=? AND key!=?",
                (row["canonical_id"], Status.ACTIVE.value, key), 100)
            for r in rivals:
                self.vault.set_status(r["key"], Status.ARCHIVED.value, now, archived_at=now)
                retired.append(r["key"])
        self.vault.conn.execute(
            "UPDATE memories SET status=?, updated_at=?, archived_at=NULL WHERE key=?",
            (Status.ACTIVE.value, now, key),
        )
        self.vault.conn.commit()
        self._audit("restore", {"key": key, "retired": retired})
        return {"key": key, "status": "active", "retired": retired}

    def purge_memory(self, canonical_id: str) -> dict:
        n = self.vault.delete_by_canonical(canonical_id)
        self._audit("purge", {"canonical_id": canonical_id, "deleted": n})
        return {"canonical_id": canonical_id, "deleted": n}

    # -- lifecycle ---------------------------------------------------------------
    def gc(self, dry_run: bool = True) -> dict:
        """Promote stale superseded → archived (7d), delete archived (30d) + expired.

        DRY_RUN defaults on — reports, deletes nothing. Circuit breaker aborts
        real runs that would delete > max(10, 5% of the vault).
        """
        now = now_epoch()
        total = self.vault.count()
        promote = [
            r["key"] for r in self.vault.scan("status=? AND updated_at<=?", (Status.SUPERSEDED.value, now - 7 * 86400), 10000)
        ]
        doomed = {r["key"] for r in self.vault.scan("status=? AND archived_at IS NOT NULL AND archived_at<=?", (Status.ARCHIVED.value, now - 30 * 86400), 10000)}
        doomed |= {r["key"] for r in self.vault.scan("expires_at IS NOT NULL AND expires_at<=?", (now,), 10000)}
        breaker = len(doomed) > max(10, int(total * 0.05))
        result: dict = {"promoted": len(promote), "to_delete": len(doomed),
                        "dry_run": dry_run, "breaker_tripped": breaker}
        if dry_run:
            result["promote_keys"] = promote[:20]
            return result
        if breaker:
            result["aborted"] = True
            return result
        for k in promote:
            self.vault.set_status(k, Status.ARCHIVED.value, now, archived_at=now)
        deleted = self.vault.delete_by_keys(sorted(doomed))
        orphans = self.vault.sweep_orphan_docs()
        self._audit("gc", {"promoted": len(promote), "deleted": deleted,
                           "orphan_docs": orphans})
        result["deleted"] = deleted
        result["orphan_docs"] = orphans
        return result

    # -- stats -----------------------------------------------------------------
    def stats(self) -> dict:
        by_status = {
            r["status"]: r["c"]
            for r in self.vault.conn.execute("SELECT status, COUNT(*) c FROM memories GROUP BY status").fetchall()
        }
        return {"total": self.vault.count(), "by_status": by_status,
                "agent": self.agent_id, "embedder": self.embedder.name,
                "dims": self.embedder.dims, "docs": self.vault.doc_stats()}

    # -- sync packs (git-native team mode) -----------------------------------------
    def export(self, since: int | None = None) -> dict:
        where, args = ("", ()) if since is None else ("updated_at>=?", (since,))
        rows = self.vault.scan(where, args, 100000)
        mems = []
        for r in rows:
            d = dict(r)
            blob = d.pop("embedding")
            d["embedding"] = base64.b64encode(bytes(blob)).decode("ascii")
            d.pop("rowid", None)
            d.pop("content_ref", None)  # packs carry full content; importer re-spills
            d["content"] = self.vault.read_content(r)
            mems.append(d)
        return {"pack": "cairn-export-1", "embed_model": self.embedder.name,
                "dims": self.embedder.dims, "exported_at": now_epoch(), "memories": mems}

    def import_pack(self, pack: dict) -> dict:
        if (not isinstance(pack, dict) or not isinstance(pack.get("memories"), list)
                or "embed_model" not in pack or "dims" not in pack):
            raise ValueError(
                "invalid pack: need {embed_model, dims, memories[]} from `cairn export`")
        if pack.get("embed_model") != self.embedder.name or pack.get("dims") != self.embedder.dims:
            raise ValueError(
                f"pack is {pack.get('embed_model')}/{pack.get('dims')}d, vault is "
                f"{self.embedder.name}/{self.embedder.dims}d — refusing cross-space import"
            )
        added, skipped = 0, 0
        for m in pack.get("memories", []):
            if self.vault.get(m["key"]) is not None:
                skipped += 1
                continue
            vec = _decode_embedding(m.pop("embedding"))
            self.vault.insert(m, vec)
            added += 1
        self._audit("import", {"added": added, "skipped": skipped})
        return {"added": added, "skipped": skipped}
