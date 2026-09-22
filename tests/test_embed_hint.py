"""Embedder hint: spec + optional dims so FastEmbed can skip the probe embed."""
import io
import json
from cairn.embed import FastEmbedder, format_embedder_hint, parse_embedder_hint


def test_parse_legacy_one_line():
    assert parse_embedder_hint("fastembed\n") == ("fastembed", None)
    assert parse_embedder_hint("hash") == ("hash", None)


def test_parse_spec_and_dims():
    assert parse_embedder_hint("fastembed\n384\n") == ("fastembed", 384)
    assert format_embedder_hint("fastembed", 384) == "fastembed\n384\n"


def test_fastembed_skips_probe_when_dims_cached(monkeypatch):
    calls = {"embed": 0}

    class FakeTE:
        def __init__(self, model):
            self.model = model

        def embed(self, texts):
            calls["embed"] += 1
            return [[0.0] * 384 for _ in texts]

    import sys
    import types

    mod = types.ModuleType("fastembed")
    mod.TextEmbedding = FakeTE
    monkeypatch.setitem(sys.modules, "fastembed", mod)

    e = FastEmbedder(dims=384)
    assert e.dims == 384
    assert calls["embed"] == 0
    e.embed(["hello"])
    assert calls["embed"] == 1


def test_ollama_sends_one_batch(monkeypatch):
    from cairn.embed import OllamaEmbedder

    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def fake_urlopen(req, timeout=0):
        seen["body"] = json.loads(req.data.decode())
        seen["timeout"] = timeout
        return Resp(json.dumps({"embeddings": [[1.0, 0.0], [0.0, 1.0]]}).encode())

    monkeypatch.setattr("cairn.embed.urllib.request.urlopen", fake_urlopen)
    emb = OllamaEmbedder(dims=2)
    out = emb.embed(["alpha", "beta"])
    assert seen["body"]["input"] == ["alpha", "beta"]
    assert out.shape == (2, 2)
