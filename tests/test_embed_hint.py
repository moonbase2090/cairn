"""Embedder hint: spec + optional dims so FastEmbed can skip the probe embed."""
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
