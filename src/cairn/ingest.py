"""Seed a vault from a docs directory — chunked per section, idempotent.

Each markdown file becomes one or more `document` memories (task_id = file stem),
chunked at ## headings so sections stay precisely retrievable. Re-runs are safe:
unchanged chunks return `unchanged` via the exact-hash path.
"""
from __future__ import annotations

import re
from pathlib import Path

from cairn.models import content_digest

HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
SUFFIXES = (".md", ".markdown", ".txt")
MAX_FILE_BYTES = 1_000_000
MAX_CHUNK_CHARS = 20_000


def sanitize_task_id(stem: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return slug or "doc"


def chunk_markdown(text: str) -> list[tuple[str | None, str]]:
    """Split into (heading, body) chunks at H1–H3 lines; oversized chunks split by paragraph."""
    chunks: list[tuple[str | None, str]] = []
    heading: str | None = None
    buf: list[str] = []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            chunks.append((heading, body))

    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            flush()
            heading = m.group(2).strip()
            buf = []
        else:
            buf.append(line)
    flush()

    out: list[tuple[str | None, str]] = []
    for h, body in chunks:
        if len(body) <= MAX_CHUNK_CHARS:
            out.append((h, body))
            continue
        # split long sections by blank lines, keeping the heading on each piece
        piece: list[str] = []
        size = 0
        for para in body.split("\n\n"):
            if size + len(para) > MAX_CHUNK_CHARS and piece:
                out.append((h, "\n\n".join(piece).strip()))
                piece, size = [], 0
            piece.append(para)
            size += len(para)
        if "\n\n".join(piece).strip():
            out.append((h, "\n\n".join(piece).strip()))
    return out


def ingest_dir(client, team: str, path: str | Path, memory_type: str = "document") -> dict:
    root = Path(path)
    if not root.is_dir():
        raise ValueError(f"not a directory: {path}")
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUFFIXES and not any(part.startswith(".") for part in p.parts)
    )
    created = unchanged = flagged = 0
    flagged_keys: list[dict] = []
    for fp in files:
        try:
            if fp.stat().st_size > MAX_FILE_BYTES:
                continue
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.strip():
            continue
        rel = str(fp.relative_to(root))
        task_id = sanitize_task_id(fp.stem)
        pending: list[tuple[str, str | None]] = []
        for heading, body in chunk_markdown(text):
            content = f"# {heading}\n\n{body}" if heading else body
            if client.vault.by_hash(f"sha256:{content_digest(content)}", task_id):
                unchanged += 1
                continue
            pending.append((content, heading))
        if not pending:
            continue
        vecs = client.embedder.embed([c for c, _h in pending])
        for (content, heading), vec in zip(pending, vecs):
            res = client.store_memory(
                content, team_id=team, task_id=task_id, memory_type=memory_type,
                provenance=rel, vector=vec,
            )
            if res.action.value == "created":
                created += 1
            elif res.action.value == "unchanged":
                unchanged += 1
            else:  # duplicate_detected — surface, don't silently drop
                flagged += 1
                flagged_keys.append({"file": rel, "heading": heading,
                                     "near": [m.key for m in res.near_duplicates]})
    return {"files": len(files), "created": created, "unchanged": unchanged,
            "flagged": flagged, "flagged_keys": flagged_keys}
