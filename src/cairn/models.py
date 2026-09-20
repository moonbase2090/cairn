"""Core data model for cairn — append-only versioned memory records.

Filterable vs content split, deterministic keys, append-only versioning.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum

_WS = re.compile(r"\s+")


class MemoryType(str, Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    DOCUMENT = "document"
    CHUNK = "chunk"


class Status(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class Origin(str, Enum):
    AGENT = "agent"
    EXTERNAL = "external"


class StoreAction(str, Enum):
    CREATED = "created"
    SUPERSEDED = "superseded"
    UNCHANGED = "unchanged"
    DUPLICATE_DETECTED = "duplicate_detected"


def normalize_text(text: str) -> str:
    return _WS.sub(" ", text.strip().lower())


def content_digest(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def build_key(agent_id: str, task_id: str, digest_hex: str, version: int) -> str:
    """Deterministic key — identical (agent, task, content, version) re-stores idempotently."""
    return f"mem_{agent_id}_{task_id}_{digest_hex[:16]}_v{version}"


def now_epoch() -> int:
    return int(time.time())


@dataclass
class MemoryRecord:
    key: str
    canonical_id: str
    content: str | None = None
    content_summary: str | None = None
    memory_type: str = "semantic"
    status: str = "active"
    origin: str = "agent"
    task_id: str = ""
    agent_id: str = ""
    team_id: str = ""
    version: int = 1
    created_at: int = 0
    expires_at: int | None = None
    confidence: float | None = None
    provenance: str | None = None
    content_hash: str | None = None
    distance: float | None = None  # cosine distance (similarity = 1 - distance)
    similarity: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StoreResult:
    key: str | None
    version: int | None
    action: StoreAction
    canonical_id: str | None = None
    near_duplicates: list[MemoryRecord] = field(default_factory=list)
    warning: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["action"] = self.action.value
        d["near_duplicates"] = [m.to_dict() for m in self.near_duplicates]
        return d
