"""Pluggable embedders. Default is offline + keyless; upgrades are opt-in.

- HashEmbedder: deterministic feature hash (token + character trigram).
  Zero deps beyond numpy, zero downloads. Exact matches score 1.0.
  Name is hash-v2. A hash-v1 vault does not open: the spaces differ.
- FastEmbedder: `pip install cairn[embed]`, ONNX BGE model, still keyless/local.
  CLI/MCP prefer `cairn-embedd` (one ONNX process) and fall back in-process.
- OllamaEmbedder: local Ollama server (`ollama pull mxbai-embed-large`), keyless.

Each collection is tagged with its embed model + dims; vectors from different
spaces are never compared (cross-space queries are refused at open/import).
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request

import numpy as np

from .models import content_digest  # noqa: F401  (re-export for convenience)


class Embedder:
    name: str = "base"
    dims: int = 0

    def embed(self, texts: list[str]) -> np.ndarray:
        raise NotImplementedError


class HashEmbedder(Embedder):
    """Deterministic offline embedder — no downloads, no keys, no server.

    Feature hashing: each token and character trigram adds +1 or -1 into one
    bin. This is hash-v2. Do not compare these vectors with hash-v1 vaults.
    """

    name = "hash-v2"

    def __init__(self, dims: int = 384):
        self.dims = dims

    def _accumulate(self, vec: np.ndarray, blob: bytes) -> None:
        digest = hashlib.blake2s(blob, digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "little") % self.dims
        vec[idx] += 1.0 if digest[4] & 1 else -1.0

    def _one(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dims, dtype=np.float32)
        raw = text.strip().lower()
        for tok in raw.split():
            self._accumulate(vec, tok.encode())
        encoded = raw.encode()
        for i in range(max(0, len(encoded) - 2)):
            self._accumulate(vec, encoded[i:i + 3])
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return vec

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dims), dtype=np.float32)
        return np.stack([self._one(t) for t in texts]).astype(np.float32)


class FastEmbedder(Embedder):
    name = "fastembed-bge-small"

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5", dims: int | None = None):
        try:
            from fastembed import TextEmbedding  # lazy — optional dep
        except ImportError:
            raise ImportError(
                'fastembed is not installed — run `pip install -e ".[embed]"` '
                "or init with `--embed-spec hash`"
            ) from None

        self._model = TextEmbedding(model)
        self.model_id = model
        if dims:
            self.dims = int(dims)
        else:
            probe = list(self._model.embed(["probe"]))[0]
            self.dims = len(probe)

    def embed(self, texts: list[str]) -> np.ndarray:
        vecs = list(self._model.embed(texts))
        arr = np.stack([np.asarray(v, dtype=np.float32) for v in vecs])
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (arr / norms).astype(np.float32)


class OllamaEmbedder(Embedder):
    def __init__(self, model: str = "mxbai-embed-large", host: str = "http://localhost:11434",
                 dims: int | None = None):
        self.model_id = model
        self.name = f"ollama-{model}"
        self.host = host.rstrip("/")
        self.dims = int(dims) if dims else len(self.embed(["probe"])[0])

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dims or 0), dtype=np.float32)
        req = urllib.request.Request(
            f"{self.host}/api/embed",
            data=json.dumps({"model": self.model_id, "input": list(texts)}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.load(resp)
        rows = body["embeddings"]
        if len(rows) != len(texts):
            raise RuntimeError(
                f"ollama returned {len(rows)} vectors for {len(texts)} inputs"
            )
        arr = np.stack([np.asarray(v, dtype=np.float32) for v in rows])
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (arr / norms).astype(np.float32)


def parse_embedder_hint(text: str) -> tuple[str, int | None]:
    """Hint file: line 1 is spec (`fastembed`/`hash`/`ollama`), optional line 2 is dims."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "hash", None
    spec = lines[0]
    dims = None
    if len(lines) > 1 and lines[1].isdigit():
        dims = int(lines[1])
    return spec, dims


def format_embedder_hint(spec: str, dims: int | None = None) -> str:
    return f"{spec}\n{dims}\n" if dims else f"{spec}\n"


def _socket_disabled() -> bool:
    return os.environ.get("CAIRN_EMBEDD", "1").strip().lower() in {"0", "off", "false", "no"}


def get_embedder(spec: str, dims: int | None = None, skip_socket: bool = False) -> Embedder:
    """`hash` (default) | `fastembed[:model]` | `ollama[:model]`.

    Pass cached `dims` to skip the FastEmbed/Ollama probe embed on process start.
    Fastembed uses cairn-embedd when the socket is up (or can be spawned),
    unless skip_socket=True (the daemon itself) or CAIRN_EMBEDD=0.
    Hash stays in-process. Vault BLOB+vec0 storage is unchanged.
    """
    if spec == "hash" or spec.startswith("hash"):
        return HashEmbedder(dims=dims or 384)
    if spec == "fastembed" or spec.startswith("fastembed:"):
        if not skip_socket and not _socket_disabled():
            try:
                from cairn.embedd import SocketEmbedder, ping, sock_path, spawn

                path = sock_path()
                if not ping(path):
                    spawn(spec, path)
                se = SocketEmbedder(spec, path=path, dims=dims)
                se.info()
                return se
            except Exception as exc:
                import sys
                sys.stderr.write(
                    f"cairn-embedd unavailable ({exc}); loading fastembed in-process\n"
                )
        model = spec.split(":", 1)[1] if ":" in spec else "BAAI/bge-small-en-v1.5"
        return FastEmbedder(model, dims=dims)
    if spec == "ollama" or spec.startswith("ollama:"):
        model = spec.split(":", 1)[1] if ":" in spec else "mxbai-embed-large"
        return OllamaEmbedder(model, dims=dims)
    raise ValueError(f"unknown embedder spec: {spec!r} (want hash|fastembed|ollama)")
