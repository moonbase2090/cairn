from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

LAMBDA_DIR = Path(__file__).resolve().parents[1] / "aws" / "infra" / "lambda"
sys.path.insert(0, str(LAMBDA_DIR))
authorizer = importlib.import_module("authorizer")
sync_api = importlib.import_module("sync_api")


VAULT_ID = "0123456789abcdef0123456789abcdef"
CALLER = {"vault_id": VAULT_ID, "token_id": "ct-member", "agent_id": "agent-a", "curator": False}


class FakeTokenTable:
    def __init__(self, item):
        self.item = item
        self.calls = []

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        return {"Item": self.item} if self.item is not None else {}


class FakeVault:
    def __init__(self):
        self.vault_identity = SimpleNamespace(vault_id=VAULT_ID, name="private team")
        self.cursors = {("agent-b-origin", "pull", "ct-member"): 10}
        self.events = [{"event_id": "agent-b:1", "feed_seq": 11}]
        self.cursor_writes = []
        self.existing = {}

    def count(self):
        return 3

    def has_sync_event(self, _event_id):
        return False

    def get(self, key):
        return self.existing.get(key)

    def get_sync_cursor(self, peer, direction, token_id):
        return self.cursors.get((peer, direction, token_id), 0)

    def set_sync_cursor(self, peer, direction, cursor, updated_at, token_id):
        value = (peer, direction, cursor, updated_at, token_id)
        self.cursor_writes.append(value)
        self.cursors[(peer, direction, token_id)] = cursor

    @contextmanager
    def transaction(self):
        yield

    def export_sync_events(self, after, limit):
        events = [event for event in self.events if event["feed_seq"] > after][:limit]
        return {
            "pack": "cairn-sync-2", "after": after,
            "cursor": events[-1]["feed_seq"] if events else after,
            "events": events,
        }

    def knn(self, query, limit, status, filters):
        self.query = (query.tolist(), limit, status, filters)
        return [({"key": "mem_agent-a_task_digest_1"}, 0.1)]


def fake_client(vault=None):
    store = vault or FakeVault()
    return SimpleNamespace(
        vault=store,
        embedder=SimpleNamespace(name="hash", dims=3),
        import_sync_pack=lambda pack, **kwargs: {"cursor": pack["cursor"], **kwargs},
        _record=lambda row, similarity: SimpleNamespace(
            to_dict=lambda: {**row, "similarity": similarity},
        ),
    )


def event(path, method, caller=CALLER, *, body=None, headers=None, query=None):
    result = {
        "rawPath": path,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"lambda": caller},
        },
        "headers": headers or {},
    }
    if body is not None:
        result["body"] = json.dumps(body)
    if query is not None:
        result["queryStringParameters"] = query
    return result


def response_body(response):
    return json.loads(response["body"])


def test_authorizer_looks_up_only_the_vault_qualified_token_digest():
    token = "cairn_example_secret_token"
    digest = hashlib.sha256(token.encode()).hexdigest()
    table = FakeTokenTable({
        "recordType": "server-token", "vault_id": VAULT_ID,
        "token_id": "ct-member", "agent_id": "agent-a", "curator": False,
        "revoked": False,
    })

    result = authorizer.authorize_request(
        {"headers": {"Authorization": f"Bearer {token}"}}, table, VAULT_ID,
    )

    assert result == {"isAuthorized": True, "context": CALLER}
    assert table.calls == [{
        "Key": {"PK": f"VAULT#{VAULT_ID}#TOKEN#{digest}", "SK": "TOKEN"},
        "ConsistentRead": True,
    }]
    assert token not in json.dumps(result)
    assert digest not in json.dumps(result)


def test_authorizer_rejects_revoked_and_wrong_vault_memberships():
    event_data = {"headers": {"authorization": "Bearer cairn_token"}}
    revoked = FakeTokenTable({
        "recordType": "server-token", "vault_id": VAULT_ID,
        "token_id": "ct-member", "agent_id": "agent-a", "curator": False,
        "revoked": True,
    })
    wrong_vault = FakeTokenTable({
        "recordType": "server-token", "vault_id": "f" * 32,
        "token_id": "ct-member", "agent_id": "agent-a", "curator": False,
        "revoked": False,
    })

    assert authorizer.authorize_request(event_data, revoked, VAULT_ID) == {"isAuthorized": False}
    assert authorizer.authorize_request(event_data, wrong_vault, VAULT_ID) == {"isAuthorized": False}
    assert authorizer.authorize_request({}, wrong_vault, VAULT_ID) == {"isAuthorized": False}


def test_health_uses_the_stable_endpoint_identity():
    client = fake_client()
    request = event("/health", "GET")

    response = sync_api.handle_request(
        request, client, endpoint_origin_id=f"aws-{VAULT_ID}", embed_dims=3,
    )

    assert response["statusCode"] == 200
    assert response_body(response) == {
        "ok": True, "memories": 3, "vault_id": VAULT_ID,
        "vault_name": "private team", "origin_id": f"aws-{VAULT_ID}",
        "token_id": "ct-member",
    }


def test_pull_paginates_and_saves_a_cursor_for_the_member_token():
    vault = FakeVault()
    request = event(
        "/pull", "GET", headers={"x-cairn-peer": "agent-b-origin"},
        query={"after": "10", "limit": "5"},
    )

    response = sync_api.handle_request(
        request, fake_client(vault), endpoint_origin_id=f"aws-{VAULT_ID}",
        embed_dims=3, exported_at=123,
    )

    assert response["statusCode"] == 200
    pack = response_body(response)
    assert pack == {
        "pack": "cairn-sync-2", "after": 10, "cursor": 11,
        "events": [{"event_id": "agent-b:1", "feed_seq": 11}],
        "embed_model": "hash", "dims": 3, "exported_at": 123,
        "origin_id": f"aws-{VAULT_ID}", "vault_id": VAULT_ID,
    }
    assert vault.cursor_writes == [("agent-b-origin", "pull", 11, 123, "ct-member")]


def test_push_rejects_foreign_owner_and_accepts_assigned_agent():
    pack = {
        "pack": "cairn-sync-2", "origin_id": "agent-origin", "cursor": 1,
        "events": [{
            "event_id": "agent-origin:1", "kind": "snapshot",
            "key": "mem_agent-a_task_hash_1",
            "snapshot": {"agent_id": "agent-a", "supersedes": None},
        }],
    }
    client = fake_client()
    accepted = sync_api.handle_request(
        event("/push", "POST", body=pack), client,
        endpoint_origin_id=f"aws-{VAULT_ID}", embed_dims=3,
    )
    pack["events"][0]["snapshot"]["agent_id"] = "agent-b"
    rejected = sync_api.handle_request(
        event("/push", "POST", body=pack), client,
        endpoint_origin_id=f"aws-{VAULT_ID}", embed_dims=3,
    )

    assert accepted["statusCode"] == 200
    assert response_body(accepted)["peer"] == "agent-origin"
    assert rejected["statusCode"] == 403


def test_search_validates_vector_dimensions_and_returns_vault_identity():
    vault = FakeVault()
    request = event("/search", "POST", body={"vector": [1, 0, 0], "limit": 2})

    response = sync_api.handle_request(
        request, fake_client(vault), endpoint_origin_id=f"aws-{VAULT_ID}", embed_dims=3,
    )
    invalid = sync_api.handle_request(
        event("/search", "POST", body={"vector": [1, 0]}), fake_client(vault),
        endpoint_origin_id=f"aws-{VAULT_ID}", embed_dims=3,
    )

    assert response["statusCode"] == 200
    assert response_body(response) == {
        "results": [{"key": "mem_agent-a_task_digest_1", "similarity": 0.9}],
        "vault_id": VAULT_ID, "vault_name": "private team",
    }
    assert vault.query == ([1.0, 0.0, 0.0], 2, "active", None)
    assert invalid["statusCode"] == 400


@pytest.mark.parametrize(
    "payload,expected_status",
    [
        ({"vector": "1,0,0"}, 400),
        ({"vector": [1, 0]}, 400),
        ({"vector": [True, 0, 0]}, 400),
        ({"vector": ["1", 0, 0]}, 400),
        ({"vector": [float("inf"), 0, 0]}, 400),
        ({"vector": [float("nan"), 0, 0]}, 400),
        ({"vector": [10**400, 0, 0]}, 400),
        ({"vector": [1, 0, 0], "limit": True}, 400),
        ({"vector": [1, 0, 0], "limit": 0}, 400),
        ({"vector": [1, 0, 0], "limit": 21}, 400),
        ({"vector": [1, 0, 0], "status": "missing"}, 400),
        ({"vector": [1, 0, 0], "filters": []}, 400),
        ({"vector": [1, 0, 0], "filters": {"unknown": "x"}}, 400),
        ({"vector": [1, 0, 0], "filters": {"team_id": 1}}, 400),
    ],
)
def test_search_rejects_invalid_vector_and_query_options(payload, expected_status):
    response = sync_api.handle_request(
        event("/search", "POST", body=payload), fake_client(),
        endpoint_origin_id=f"aws-{VAULT_ID}", embed_dims=3,
    )

    assert response["statusCode"] == expected_status


@pytest.mark.parametrize(
    "query,headers,expected_status",
    [
        ({"after": "bad"}, {"x-cairn-peer": "peer"}, 400),
        ({"limit": "bad"}, {"x-cairn-peer": "peer"}, 400),
        ({"after": "-1"}, {"x-cairn-peer": "peer"}, 400),
        ({"limit": "0"}, {"x-cairn-peer": "peer"}, 400),
        ({"limit": "101"}, {"x-cairn-peer": "peer"}, 400),
        ({}, {}, 400),
        ({}, {"X-Cairn-Peer": ""}, 400),
        ({"after": "11"}, {"x-cairn-peer": "agent-b-origin"}, 409),
    ],
)
def test_pull_rejects_invalid_cursors_limits_and_peer_headers(query, headers, expected_status):
    response = sync_api.handle_request(
        event("/pull", "GET", headers=headers, query=query), fake_client(),
        endpoint_origin_id=f"aws-{VAULT_ID}", embed_dims=3, exported_at=123,
    )

    assert response["statusCode"] == expected_status


def test_pull_accepts_raw_query_string_and_drops_events_to_fit_response(monkeypatch):
    vault = FakeVault()
    vault.events = [
        {"event_id": "a:1", "feed_seq": 1, "content": "x" * 200},
        {"event_id": "a:2", "feed_seq": 2, "content": "y" * 200},
    ]
    request = event("/pull", "GET", headers={"x-cairn-peer": "agent-b-origin"})
    request["rawQueryString"] = "after=0&after=0&limit=5"
    monkeypatch.setattr(sync_api, "MAX_RESPONSE_BYTES", 550)

    response = sync_api.handle_request(
        request, fake_client(vault), endpoint_origin_id=f"aws-{VAULT_ID}",
        embed_dims=3, exported_at=123,
    )

    assert response["statusCode"] == 200
    pack = response_body(response)
    assert [item["feed_seq"] for item in pack["events"]] == [1]
    assert pack["cursor"] == 1
    assert vault.cursor_writes[-1][2] == 1


def test_pull_rejects_a_single_event_larger_than_the_response_limit(monkeypatch):
    vault = FakeVault()
    vault.events = [{"event_id": "a:1", "feed_seq": 1, "content": "x" * 1000}]
    monkeypatch.setattr(sync_api, "MAX_RESPONSE_BYTES", 250)

    response = sync_api.handle_request(
        event("/pull", "GET", headers={"x-cairn-peer": "agent-b-origin"}),
        fake_client(vault), endpoint_origin_id=f"aws-{VAULT_ID}", embed_dims=3,
        exported_at=123,
    )

    assert response["statusCode"] == 413
    assert "one sync event" in response_body(response)["error"]
    assert vault.cursor_writes == []


@pytest.mark.parametrize(
    "pack,expected_status",
    [
        ({"origin_id": ""}, 400),
        ({"origin_id": "x" * 129}, 400),
        ({"origin_id": "ok", "after": 1, "events": []}, 409),
    ],
)
def test_push_rejects_invalid_origin_and_skipped_cursor(pack, expected_status):
    response = sync_api.handle_request(
        event("/push", "POST", body=pack), fake_client(),
        endpoint_origin_id=f"aws-{VAULT_ID}", embed_dims=3,
    )

    assert response["statusCode"] == expected_status


def test_push_rejects_invalid_json_and_inconsistent_authorizer_metadata():
    invalid_json = event("/push", "POST")
    invalid_json["body"] = "{"
    bad_caller = event("/health", "GET", caller={**CALLER, "vault_id": "f" * 32})
    bad_dims = event("/health", "GET")

    assert sync_api.handle_request(
        invalid_json, fake_client(), endpoint_origin_id="origin", embed_dims=3,
    )["statusCode"] == 400
    assert sync_api.handle_request(
        bad_caller, fake_client(), endpoint_origin_id="origin", embed_dims=3,
    )["statusCode"] == 403
    assert sync_api.handle_request(
        bad_dims, fake_client(), endpoint_origin_id="origin", embed_dims=8,
    )["statusCode"] == 500


@pytest.mark.parametrize(
    "body_fields",
    [
        {"body": 1},
        {"body": "@@", "isBase64Encoded": True},
        {"body": "not-json"},
        {"body": "[]"},
    ],
)
def test_request_body_validation_errors_are_returned_as_client_errors(body_fields):
    event_data = event("/search", "POST")
    event_data.update(body_fields)

    response = sync_api.handle_request(
        event_data, fake_client(), endpoint_origin_id="origin", embed_dims=3,
    )

    assert response["statusCode"] == 400


def test_request_body_size_limit_and_route_errors_are_explicit(monkeypatch):
    monkeypatch.setattr(sync_api, "MAX_REQUEST_BYTES", 3)
    oversized = event("/search", "POST", body={"vector": [1, 2, 3]})
    wrong_route = event("/unavailable", "GET")
    no_path = event(None, "GET")
    no_path.pop("rawPath")
    no_path.pop("path", None)

    assert sync_api.handle_request(
        oversized, fake_client(), endpoint_origin_id="origin", embed_dims=3,
    )["statusCode"] == 413
    assert sync_api.handle_request(
        wrong_route, fake_client(), endpoint_origin_id="origin", embed_dims=3,
    )["statusCode"] == 404
    assert sync_api.handle_request(
        no_path, fake_client(), endpoint_origin_id="origin", embed_dims=3,
    )["statusCode"] == 404


def test_response_size_limit_replaces_an_oversized_payload(monkeypatch):
    monkeypatch.setattr(sync_api, "MAX_RESPONSE_BYTES", 10)

    response = sync_api._response(200, {"payload": "x" * 100})

    assert response["statusCode"] == 413
    assert response_body(response) == {"error": "response exceeds the sync API size limit"}
