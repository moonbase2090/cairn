"""Storage backend interface: everything cairn keeps in a vault's live database.

CairnClient, the MCP server, `cairn serve`, and the galaxy view talk to a
StorageBackend, never to a database driver. The default backend is SQLite
(cairn.store.Vault). Other backends register in BACKENDS and must pass the
shared contract suite in tests/test_storage_contract.py.

Rows returned by a backend are read-only mappings: ``row["key"]`` and
``dict(row)`` work, and they carry every name in MEMORY_FIELDS (plus
``embedding`` as float32 bytes when asked for). ``row["content"]`` is None
when the text was stored as a document; read_content() always returns it. The vault directory itself
(project.json, embedder hint, audit log) stays on local disk whatever the
backend; agent identity lives there, and each memory row records its agent_id.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

DEFAULT_BACKEND = "sqlite"

MEMORY_FIELDS = (
    "key", "canonical_id", "content", "content_summary", "memory_type", "status",
    "origin", "task_id", "agent_id", "team_id", "version", "created_at", "updated_at",
    "expires_at", "archived_at", "supersedes", "parent_key", "provenance",
    "confidence", "content_hash",
)

# Exact-match columns a MemoryQuery or search filter may name.
FILTER_COLUMNS = ("task_id", "canonical_id", "memory_type", "status", "team_id", "agent_id")

Row = Mapping


class UnknownBackendError(ValueError):
    pass


class SpaceMismatchError(ValueError):
    """The vault was built with a different embedder or dimension count."""


class ContentIntegrityError(ValueError):
    """A stored document is missing or fails its hash check. Loud by design —
    integrity failures must never degrade to silent wrong answers."""


@dataclass(frozen=True)
class MemoryQuery:
    """A structured scan over memories, ordered by created_at ascending.

    Every set field narrows the result (AND). Time bounds are epoch seconds
    and inclusive; archived_before and expired_by skip rows whose timestamp
    is NULL.
    """

    eq: dict = field(default_factory=dict)
    exclude_key: str | None = None
    updated_before: int | None = None
    updated_since: int | None = None
    archived_before: int | None = None
    expired_by: int | None = None

    def __post_init__(self):
        bad = set(self.eq) - set(FILTER_COLUMNS)
        if bad:
            raise ValueError(f"MemoryQuery cannot filter on {sorted(bad)}")


class StorageBackend:
    """One vault's live store: memories, keyword and vector search, documents.

    Backends subclass this and override every method; the contract suite
    checks that none is left to the NotImplementedError default.
    """

    name: str

    # -- lifecycle -----------------------------------------------------------
    @property
    def vault_dir(self) -> Path:
        """Local vault directory (project.json, embedder hint, audit.jsonl)."""
        raise NotImplementedError

    def reopen(self) -> StorageBackend:
        """A fresh handle on the same vault, e.g. for another thread."""
        raise NotImplementedError

    def transaction(self) -> AbstractContextManager:
        """One commit for the block; nested blocks join the open transaction."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    # -- memories --------------------------------------------------------------
    def insert(self, rec: dict, vector: np.ndarray) -> int:
        """Store one memory. rec["content"] is always the full text."""
        raise NotImplementedError

    def get(self, key: str) -> Row | None:
        raise NotImplementedError

    def by_hash(self, content_hash: str, task_id: str, status: str = "active") -> list[Row]:
        raise NotImplementedError

    def find(self, query: MemoryQuery, limit: int = 100,
             with_embedding: bool = False) -> list[Row]:
        raise NotImplementedError

    def set_status(self, key: str, status: str, now: int, archived_at: int | None = None) -> int:
        """Set status, updated_at, and archived_at; returns rows changed."""
        raise NotImplementedError

    def delete_by_keys(self, keys: list[str]) -> int:
        raise NotImplementedError

    def delete_by_canonical(self, canonical_id: str) -> int:
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError

    def count_by_status(self) -> dict[str, int]:
        raise NotImplementedError

    # -- search ------------------------------------------------------------------
    def fts_search(self, text: str, limit: int = 100, status: str = "active",
                   extra: dict | None = None) -> list[Row]:
        """Keyword search over content, best match first. User text is literal."""
        raise NotImplementedError

    def knn(self, query: np.ndarray, k: int, status: str = "active",
            filters: dict | None = None, now: int | None = None) -> list[tuple[Row, float]]:
        """[(row, cosine_distance)] closest first; filters and expiry apply before the cut."""
        raise NotImplementedError

    def vec_status(self) -> dict:
        """Vector index health; always carries a boolean "vec_in_sync"."""
        raise NotImplementedError

    def rebuild_vec(self) -> dict:
        raise NotImplementedError

    # -- documents -----------------------------------------------------------------
    @property
    def doc_threshold(self) -> int:
        """Content larger than this many UTF-8 bytes is stored as a document."""
        raise NotImplementedError

    def read_content(self, row: Row) -> str:
        """Full text for a row; raises ContentIntegrityError if it is missing or altered."""
        raise NotImplementedError

    def sweep_orphan_docs(self) -> int:
        raise NotImplementedError

    def doc_stats(self) -> dict:
        raise NotImplementedError


Opener = Callable[..., StorageBackend]


def _open_sqlite(vault_dir: Path, embed_name: str, dims: int, create: bool = False,
                 doc_threshold: int | None = None) -> StorageBackend:
    from .store import Vault

    return Vault(Path(vault_dir) / "vault.db", embed_name, dims, create=create,
                 doc_threshold=doc_threshold)


BACKENDS: dict[str, Opener] = {"sqlite": _open_sqlite}


def backend_names() -> Iterator[str]:
    return iter(sorted(BACKENDS))


def open_backend(vault_dir: Path, embed_name: str, dims: int, *,
                 backend: str = DEFAULT_BACKEND, create: bool = False,
                 doc_threshold: int | None = None) -> StorageBackend:
    """Open (or with create=True, initialise) a vault on the named backend."""
    try:
        opener = BACKENDS[backend]
    except KeyError:
        raise UnknownBackendError(
            f"unknown storage backend {backend!r}; available: {', '.join(backend_names())}"
        ) from None
    return opener(Path(vault_dir), embed_name, dims, create=create, doc_threshold=doc_threshold)
