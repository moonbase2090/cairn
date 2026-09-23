"""Real-embedder validation — auto-skips when the backend is unavailable.

Measured 2026-09-18 (BAAI/bge-small-en-v1.5 via fastembed, mxbai-embed-large via
ollama). These pin the quality gap over the hash default and the per-embedder
behavior of the 0.95 near-dup threshold.
"""
import numpy as np
import pytest

fastembed = pytest.importorskip("fastembed", reason="pip install cairn[embed] to run")

from cairn.client import CairnClient
from cairn.embed import FastEmbedder, OllamaEmbedder
from cairn.store import Vault

RELATED = ("Q2 revenue grew 12 percent year over year", "how did Q2 revenue do?")
NEAR_DUP = ("the quick brown fox jumps over the lazy dog", "the quick brown fox jumps over the lazy dogs")
UNRELATED = ("Q2 revenue grew 12 percent year over year", "deploy the coolant manifold before friday")


def sim(e, a, b):
    v = e.embed([a, b])
    return float(np.dot(v[0], v[1]))


def ollama_or_skip():
    try:
        return OllamaEmbedder()
    except (OSError, TimeoutError, ValueError, KeyError) as e:
        pytest.skip(f"ollama mxbai-embed-large unavailable: {e}")


def test_fastembed_quality():
    e = FastEmbedder()
    assert (e.dims, e.name) == (384, "fastembed-bge-small")
    assert sim(e, *RELATED) > 0.6  # measured 0.80 (hash: 0.33)
    assert sim(e, *UNRELATED) < 0.6  # measured 0.42
    assert sim(e, *NEAR_DUP) >= 0.95  # measured 0.97 → trips duplicate_detected


def test_fastembed_vault_roundtrip(tmp_path):
    e = FastEmbedder()
    client = CairnClient(Vault(tmp_path / "vault.db", e.name, e.dims, create=True), "fe-test", e)
    res = client.store_memory(RELATED[0], team_id="t", task_id="k")
    hits = client.retrieve_memory(RELATED[1], filters={"task_id": "k"})
    assert hits and hits[0].key == res.key and hits[0].similarity > 0.6
    dup = client.store_memory(NEAR_DUP[1], team_id="t", task_id="k")
    assert dup.action.value == "created"  # first sight: no near rival yet
    dup2 = client.store_memory(NEAR_DUP[0], team_id="t", task_id="k")
    assert dup2.action.value == "duplicate_detected"  # 0.97 ≥ 0.95 screen


def test_ollama_quality():
    e = ollama_or_skip()
    assert (e.dims, e.name) == (1024, "ollama-mxbai-embed-large")
    assert sim(e, *RELATED) > 0.6  # measured 0.75 (hash: 0.33)
    assert sim(e, *UNRELATED) < 0.6  # measured 0.31 — best separation of the three
    assert 0.8 < sim(e, *NEAR_DUP) < 0.95  # measured 0.92: no false block, explicit supersede still works


def test_ollama_vault_roundtrip(tmp_path):
    e = ollama_or_skip()
    client = CairnClient(Vault(tmp_path / "vault.db", e.name, e.dims, create=True), "ol-test", e)
    res = client.store_memory(RELATED[0], team_id="t", task_id="k")
    hits = client.retrieve_memory(RELATED[1], filters={"task_id": "k"})
    assert hits and hits[0].key == res.key and hits[0].similarity > 0.6
