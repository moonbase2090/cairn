"""Unit tests for PostgreSQL sync-event validation without a live database."""

from __future__ import annotations

import pytest

from cairn.postgres import PostgresVault

BASE_KEY = "base-memory"
COMPETITORS = ["correction-a", "correction-b"]
WINNER_KEY = "correction-b"


def make_backend(monkeypatch, *, stored=(), dynamic=(), rows=None):
    backend = object.__new__(PostgresVault)
    monkeypatch.setattr(
        backend,
        "list_sync_conflicts",
        lambda include_resolved=False: list(stored),
    )
    monkeypatch.setattr(
        backend,
        "list_competing_corrections",
        lambda: list(dynamic),
    )
    monkeypatch.setattr(backend, "get", lambda key: (rows or {}).get(key))
    return backend


def valid_event():
    return {
        "kind": "state",
        "key": "correction-a",
        "status": "superseded",
    }


def valid_resolution():
    return {
        "base_key": BASE_KEY,
        "winner_key": WINNER_KEY,
        "competitor_keys": COMPETITORS.copy(),
        "resolved_by": "agent-a",
    }


def matching_conflict():
    return {
        "base_key": BASE_KEY,
        "competitor_keys": COMPETITORS.copy(),
    }


def matching_rows():
    return {
        "correction-a": {"supersedes": BASE_KEY},
        WINNER_KEY: {"supersedes": BASE_KEY, "status": "active"},
    }


@pytest.mark.parametrize(
    ("event", "resolution", "message"),
    [
        ({"kind": "snapshot", "status": "superseded"}, {}, "supersede state event"),
        ({"kind": "state", "status": "archived"}, {}, "supersede state event"),
        (valid_event(), None, "supersede state event"),
    ],
)
def test_rejects_non_supersede_resolution_events(
    monkeypatch, event, resolution, message
):
    backend = make_backend(monkeypatch)

    with pytest.raises(ValueError, match=message):
        backend._validate_resolution_event(event, resolution)


@pytest.mark.parametrize(
    ("event", "resolution"),
    [
        (valid_event(), {**valid_resolution(), "base_key": None}),
        (valid_event(), {**valid_resolution(), "winner_key": None}),
        (valid_event(), {**valid_resolution(), "competitor_keys": None}),
        (valid_event(), {**valid_resolution(), "competitor_keys": ["correction-a"]}),
        (
            valid_event(),
            {**valid_resolution(), "competitor_keys": ["correction-a", 4]},
        ),
        (
            valid_event(),
            {
                **valid_resolution(),
                "competitor_keys": ["correction-a", WINNER_KEY, WINNER_KEY],
            },
        ),
        (valid_event(), {**valid_resolution(), "resolved_by": None}),
        (valid_event(), {**valid_resolution(), "resolved_by": ""}),
        ({**valid_event(), "key": "outside"}, valid_resolution()),
        (
            valid_event(),
            {**valid_resolution(), "winner_key": "outside"},
        ),
        (
            {**valid_event(), "key": WINNER_KEY},
            valid_resolution(),
        ),
    ],
)
def test_rejects_malformed_resolution_members(monkeypatch, event, resolution):
    backend = make_backend(monkeypatch)

    with pytest.raises(ValueError, match="invalid competing correction resolution"):
        backend._validate_resolution_event(event, resolution)


@pytest.mark.parametrize(
    ("stored", "dynamic", "rows"),
    [
        ([], [], matching_rows()),
        (
            [{"base_key": "another-base", "competitor_keys": COMPETITORS}],
            [],
            matching_rows(),
        ),
        (
            [],
            [{"base_key": BASE_KEY, "competitor_keys": ["other-a", "other-b"]}],
            matching_rows(),
        ),
        ([matching_conflict()], [], {"correction-a": {"supersedes": BASE_KEY}}),
        (
            [matching_conflict()],
            [],
            {
                "correction-a": {"supersedes": BASE_KEY},
                WINNER_KEY: {"supersedes": BASE_KEY, "status": "superseded"},
            },
        ),
        (
            [matching_conflict()],
            [],
            {
                "correction-a": {"supersedes": BASE_KEY},
                WINNER_KEY: {"supersedes": "different-base", "status": "active"},
            },
        ),
    ],
)
def test_rejects_resolutions_without_matching_active_corrections(
    monkeypatch, stored, dynamic, rows
):
    backend = make_backend(monkeypatch, stored=stored, dynamic=dynamic, rows=rows)

    with pytest.raises(
        ValueError,
        match="conflict resolution does not match an active competing correction",
    ):
        backend._validate_resolution_event(valid_event(), valid_resolution())


@pytest.mark.parametrize("conflict_source", ["stored", "dynamic"])
def test_accepts_resolution_for_matching_active_corrections(
    monkeypatch, conflict_source
):
    stored = [matching_conflict()] if conflict_source == "stored" else []
    dynamic = [matching_conflict()] if conflict_source == "dynamic" else []
    backend = make_backend(
        monkeypatch,
        stored=stored,
        dynamic=dynamic,
        rows=matching_rows(),
    )

    assert backend._validate_resolution_event(valid_event(), valid_resolution()) is None
