from __future__ import annotations

from copy import deepcopy
from io import BytesIO
from pathlib import Path

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
import numpy as np
import pytest

from cairn.aws_storage import AwsVault
from cairn.aws_control import _migrate_sqlite_to_aws, _migration_preflight
from cairn.models import content_digest
from cairn.secret_scan import SecretAdmissionError
from cairn.storage import StorageConfig
from cairn.store import Vault
from cairn.vault_identity import VaultIdentity


VAULT_ID = "0123456789abcdef0123456789abcdef"


class FakeAwsError(Exception):
    def __init__(self, status: int):
        super().__init__("fake AWS error")
        self.response = {"ResponseMetadata": {"HTTPStatusCode": status}}


class FakeDynamoTable:
    def __init__(self, aws, name: str):
        self.aws = aws
        self.name = name

    def get_item(self, *, Key, **_kwargs):
        self.aws.ensure_online()
        item = self.aws.items.get((Key["PK"], Key["SK"]))
        return {"Item": deepcopy(item)} if item is not None else {}

    def query(self, *, KeyConditionExpression, IndexName=None, Limit=None,
              ExclusiveStartKey=None, **_kwargs):
        self.aws.ensure_online()
        conditions = _key_conditions(KeyConditionExpression)
        partition_name, partition_value = next(
            (name, value) for name, operator, value in conditions if operator == "="
        )
        if IndexName:
            partition_name = {
                "ByVault": "GSI0PK", "ByTokenId": "GSI4PK", "ByEventId": "GSI5PK",
            }[IndexName]
        items = sorted(
            (deepcopy(item) for item in self.aws.items.values()
             if item.get(partition_name) == partition_value),
            key=lambda item: item.get("SK", ""),
        )
        for name, operator, value in conditions:
            if name == "SK" and operator == ">":
                items = [item for item in items if item.get(name, "") > value]
        if ExclusiveStartKey:
            items = [item for item in items if item["SK"] > ExclusiveStartKey["SK"]]
        if Limit and len(items) > Limit:
            page = items[:Limit]
            return {"Items": page, "LastEvaluatedKey": {
                "PK": page[-1]["PK"], "SK": page[-1]["SK"],
            }}
        return {"Items": items}

    def put_item(self, *, Item, **_kwargs):
        self.aws.ensure_online()
        item = deepcopy(Item)
        self.aws.items[(item["PK"], item["SK"])] = item
        return {}

    def delete_item(self, *, Key, ReturnValues=None, **_kwargs):
        self.aws.ensure_online()
        item = self.aws.items.pop((Key["PK"], Key["SK"]), None)
        return {"Attributes": deepcopy(item)} if ReturnValues and item else {}

    def update_item(self, *, Key, ExpressionAttributeValues, **_kwargs):
        self.aws.ensure_online()
        item = self.aws.items[(Key["PK"], Key["SK"])]
        if ":yes" in ExpressionAttributeValues:
            item["vector_projected"] = True
        return {}


def _key_conditions(condition):
    expression = condition.get_expression()
    operator = expression.get("operator")
    values = expression.get("values", ())
    if operator == "AND":
        return _key_conditions(values[0]) + _key_conditions(values[1])
    if operator in {"=", ">", ">=", "<", "<="}:
        name = values[0].name
        return [(name, operator, values[1])]
    return []


class FakeDynamoResource:
    def __init__(self, aws):
        self.aws = aws

    def Table(self, name):
        return FakeDynamoTable(self.aws, name)


class FakeS3:
    def __init__(self, aws):
        self.aws = aws
        self.objects: dict[str, bytes] = {}
        self.deleted_batches: list[list[dict]] = []

    def head_object(self, *, Key, **_kwargs):
        self.aws.ensure_online()
        if Key not in self.objects:
            raise FakeAwsError(404)
        return {}

    def put_object(self, *, Key, Body, **_kwargs):
        self.aws.ensure_online()
        self.objects[Key] = bytes(Body)
        return {}

    def get_object(self, *, Key, **_kwargs):
        self.aws.ensure_online()
        if Key not in self.objects:
            raise FakeAwsError(404)
        return {"Body": BytesIO(self.objects[Key])}

    def list_objects_v2(self, *, Prefix, **_kwargs):
        return {"Contents": [{"Key": key} for key in self.objects if key.startswith(Prefix)]}

    def list_object_versions(self, *, Prefix, **_kwargs):
        return {
            "Versions": [
                {"Key": key, "VersionId": "v1"}
                for key in self.objects if key.startswith(Prefix)
            ],
            "DeleteMarkers": [],
            "IsTruncated": False,
        }

    def delete_objects(self, *, Delete, **_kwargs):
        self.deleted_batches.append(deepcopy(Delete["Objects"]))
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)
        return {}


class FakeVectors:
    def __init__(self, aws):
        self.aws = aws
        self.vectors: dict[str, dict] = {}
        self.query_pages: list[dict] = []
        self.query_calls: list[dict] = []

    def put_vectors(self, *, vectors, **_kwargs):
        self.aws.ensure_online()
        for vector in vectors:
            self.vectors[vector["key"]] = deepcopy(vector)
        return {}

    def delete_vectors(self, *, keys, **_kwargs):
        for key in keys:
            self.vectors.pop(key, None)
        return {}

    def get_vectors(self, *, keys, **_kwargs):
        return {"vectors": [
            {"key": key, "data": self.vectors[key]["data"]}
            for key in keys if key in self.vectors
        ]}

    def query_vectors(self, **kwargs):
        self.query_calls.append(kwargs)
        return self.query_pages.pop(0) if self.query_pages else {"vectors": []}


class FakeAws:
    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}
        self.serializer = TypeSerializer()
        self.deserializer = TypeDeserializer()
        self.s3 = FakeS3(self)
        self.vectors = FakeVectors(self)
        self.offline = False
        self.fail_transaction = False
        self.transactions: list[list[dict]] = []

    def ensure_online(self):
        if self.offline:
            raise OSError("fake AWS outage")

    def resource(self, _name):
        return FakeDynamoResource(self)

    def client(self, name):
        if name == "s3":
            return self.s3
        if name == "s3vectors":
            return self.vectors
        if name == "dynamodb":
            return self
        raise AssertionError(name)

    def transact_write_items(self, *, TransactItems, **_kwargs):
        self.ensure_online()
        self.transactions.append(deepcopy(TransactItems))
        if self.fail_transaction:
            raise RuntimeError("fake transaction failed")
        updates = {}
        for action in TransactItems:
            if "Put" in action:
                spec = action["Put"]
                values = {name: self.deserializer.deserialize(value)
                          for name, value in spec["Item"].items()}
                updates[(values["PK"], values["SK"])] = values
            elif "Delete" in action:
                spec = action["Delete"]
                key = {name: self.deserializer.deserialize(value)
                       for name, value in spec["Key"].items()}
                updates[(key["PK"], key["SK"])] = None
            elif "Update" in action:
                spec = action["Update"]
                key = {name: self.deserializer.deserialize(value)
                       for name, value in spec["Key"].items()}
                values = {name: self.deserializer.deserialize(value)
                          for name, value in spec["ExpressionAttributeValues"].items()}
                updates[(key["PK"], key["SK"])] = {
                    "PK": key["PK"], "SK": key["SK"], "seq": values[":next"],
                }
        for key, value in updates.items():
            if value is None:
                self.items.pop(key, None)
            else:
                self.items[key] = value
        return {}


class FakeSession:
    def __init__(self, aws):
        self.aws = aws

    def resource(self, name):
        return self.aws.resource(name)

    def client(self, name):
        return self.aws.client(name)


def _config() -> StorageConfig:
    return StorageConfig(
        backend="aws", region="us-west-2", vault_id=VAULT_ID, vault_name="work-vault",
        table="cairn-memory", cache_table="cairn-cache",
        content_bucket="cairn-content", vector_bucket="cairn-vectors",
        vector_index="cairn-index", vector_index_arn="arn:aws:s3vectors:us-west-2:123456789012:index/mock",
    )


def _open(tmp_path: Path, aws: FakeAws) -> AwsVault:
    return AwsVault(
        tmp_path, "mock-embedder", 3, _config(), create=True,
        doc_threshold=4, _session=FakeSession(aws), _key_factory=Key,
        _serializer=aws.serializer,
    )


def _memory(content: str = "durable text") -> dict:
    now = 1_800_000_000
    return {
        "key": "mem_agent_0123456789abcdef",
        "canonical_id": "task-1234",
        "content": content,
        "content_summary": content[:200],
        "memory_type": "semantic",
        "status": "active",
        "origin": "agent",
        "task_id": "task",
        "agent_id": "agent",
        "team_id": "team",
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "expires_at": None,
        "archived_at": None,
        "supersedes": None,
        "parent_key": None,
        "provenance": None,
        "confidence": 0.75,
        "content_hash": f"sha256:{content_digest(content)}",
    }


def test_insert_commits_memory_event_cursor_and_vector_projection_atomically(tmp_path):
    aws = FakeAws()
    vault = _open(tmp_path, aws)
    vector = np.asarray([0.25, 0.5, 0.75], dtype=np.float32)

    vault.insert(_memory(), vector)

    key = _memory()["key"]
    stored = vault.get(key)
    assert stored and stored["content"] is None
    assert vault.read_content(stored) == "durable text"
    assert aws.vectors.vectors[key]["data"]["float32"] == vector.tolist()
    assert len(aws.transactions) == 1
    action_types = [next(iter(action)) for action in aws.transactions[0]]
    assert action_types.count("Put") == 3  # event, dedupe marker, and memory row
    assert "Update" in action_types  # the durable event-feed counter
    assert aws.items[(f"VAULT#{VAULT_ID}#META", "SYNC_COUNTER")]["seq"] == 1
    cloud_row = next(item for item in aws.items.values() if item.get("recordType") == "memory")
    assert str(cloud_row["confidence"]) == "0.75"
    event = vault.export_sync_events()
    assert len(event["events"]) == 1
    assert event["events"][0]["snapshot"]["content"] == "durable text"
    assert vault.has_sync_event(event["events"][0]["event_id"])
    transaction_count = len(aws.transactions)
    vault._commit_remote_events(event["events"])
    assert len(aws.transactions) == transaction_count
    vault.close()


def test_failed_dynamodb_transaction_rolls_back_local_cache(tmp_path):
    aws = FakeAws()
    aws.fail_transaction = True
    vault = _open(tmp_path, aws)

    with pytest.raises(RuntimeError, match="fake transaction failed"):
        vault.insert(_memory(), np.asarray([1, 0, 0], dtype=np.float32))

    assert vault.get(_memory()["key"]) is None
    assert not aws.items
    vault.close()


def test_offline_reads_are_marked_stale_and_offline_writes_fail_closed(tmp_path):
    aws = FakeAws()
    vault = _open(tmp_path, aws)
    vault.insert(_memory(), np.asarray([1, 0, 0], dtype=np.float32))
    aws.offline = True

    stale = vault.get(_memory()["key"])
    assert stale and stale.get("storage_stale") is True
    assert vault.read_content(stale) == "durable text"
    with pytest.raises(OSError, match="writes are disabled"):
        vault.insert(_memory("another memory"), np.asarray([0, 1, 0], dtype=np.float32))
    vault.close()


def test_server_token_reads_use_vault_qualified_key_and_never_expose_digest(tmp_path):
    aws = FakeAws()
    vault = _open(tmp_path, aws)
    digest = "a" * 64
    vault.create_server_token("tok-1", digest, "agent-1", 10, curator=True)

    found = vault.get_server_token(digest)
    listed = vault.list_server_tokens()
    assert found == {"token_id": "tok-1", "agent_id": "agent-1", "curator": True, "created_at": 10}
    assert listed == [found]
    assert "token_hash" not in found
    assert vault.delete_server_token("tok-1") == 1
    assert vault.get_server_token(digest) is None
    vault.close()


def test_repeated_cursor_updates_are_one_durable_monotonic_write(tmp_path):
    aws = FakeAws()
    vault = _open(tmp_path, aws)

    with vault.transaction():
        vault.set_sync_cursor("peer", "pull", 3, 20, "token")
        vault.set_sync_cursor("peer", "pull", 8, 30, "token")

    assert vault.get_sync_cursor("peer", "pull", "token") == 8
    actions = aws.transactions[-1]
    cursor_writes = [
        action["Put"] for action in actions
        if action.get("Put", {}).get("Item", {}).get("recordType", {}).get("S") == "sync-cursor"
    ]
    assert len(cursor_writes) == 1
    assert "#cursor" in cursor_writes[0]["ExpressionAttributeNames"]
    vault.close()


def test_secret_is_rejected_before_cloud_or_local_persistence(tmp_path):
    aws = FakeAws()
    vault = _open(tmp_path, aws)
    secret = "token=ghp_1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
    record = _memory("safe text")
    record.update({"content": secret, "content_summary": secret[:200], "content_hash": None})

    with pytest.raises(SecretAdmissionError) as error:
        vault.insert(record, np.asarray([1, 0, 0], dtype=np.float32))

    assert secret not in str(error.value)
    assert not aws.transactions and not aws.items and not aws.s3.objects
    assert vault._cache.get(record["key"]) is None
    vault.close()


def test_sqlite_migration_scans_and_verifies_memories_events_tokens_and_cursors(tmp_path):
    source = Vault(tmp_path / "vault.db", "mock-embedder", 3, create=True, doc_threshold=4)
    source._set_meta("vault_id", VAULT_ID)
    source._set_meta("vault_name", "work-vault")
    source._set_meta("vault_identity_migration", "1")
    source._vault_identity = VaultIdentity(VAULT_ID, "work-vault")
    source.insert(_memory(), np.asarray([0.25, 0.5, 0.75], dtype=np.float32))
    source.set_sync_cursor("peer", "pull", 7, 40, "token")
    source.create_server_token("tok-1", "a" * 64, "agent", 50, curator=True)
    assert _migration_preflight(source)["safe"] is True

    aws = FakeAws()
    destination = _open(tmp_path, aws)
    result = _migrate_sqlite_to_aws(source, destination)

    assert result == {"memories": 1, "events": 1, "cursors": 1, "tokens": 1, "conflicts": 0}
    assert destination.get(_memory()["key"])["content_hash"] == _memory()["content_hash"]
    assert destination.get_sync_cursor("peer", "pull", "token") == 7
    assert destination.get_server_token("a" * 64)["agent_id"] == "agent"
    assert destination.list_server_tokens() == [{
        "token_id": "tok-1", "agent_id": "agent", "curator": True, "created_at": 50,
    }]
    destination.close()
    source.close()


def test_migration_preflight_reports_only_secret_categories(tmp_path):
    source = Vault(tmp_path / "vault.db", "mock-embedder", 3, create=True)
    secret = "token=ghp_1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
    source.insert(_memory(secret), np.asarray([1, 0, 0], dtype=np.float32))

    result = _migration_preflight(source)

    assert result["safe"] is False
    assert result["findings"]["GitHub token"] >= 1
    assert secret not in str(result)
    source.close()


def test_sqlite_migration_rejects_backend_and_identity_mismatches():
    source = type("Source", (), {"name": "aws", "vault_identity": VaultIdentity(VAULT_ID, "work-vault")})()
    destination = type("Destination", (), {"name": "aws", "vault_identity": source.vault_identity})()
    with pytest.raises(ValueError, match="SQLite source and AWS destination"):
        _migrate_sqlite_to_aws(source, destination)

    source.name = "sqlite"
    destination.vault_identity = VaultIdentity("f" * 32, "other-vault")
    with pytest.raises(ValueError, match="identities do not match"):
        _migrate_sqlite_to_aws(source, destination)


def test_migration_skips_a_memory_that_already_exists_in_aws():
    row = _memory()

    class QueryResult:
        def fetchall(self):
            return []

    source = type("Source", (), {
        "name": "sqlite", "vault_identity": VaultIdentity(VAULT_ID, "work-vault"),
        "conn": type("Connection", (), {"execute": lambda *_args: QueryResult()})(),
        "export_sync_events": lambda _self, *, after, limit: {"events": [], "cursor": after},
        "iter_memories": lambda _self: [row],
        "list_sync_cursors": lambda _self: [],
        "list_sync_conflicts": lambda _self, *, include_resolved: [],
    })()
    destination = type("Destination", (), {
        "name": "aws", "vault_identity": source.vault_identity,
        "apply_sync_event": lambda *_args: None,
        "get": lambda _self, key: row if key == row["key"] else None,
        "_memory_items": lambda _self: [{"key": row["key"], "content_hash": row["content_hash"]}],
        "vec_status": lambda _self: {"vec_in_sync": True},
    })()

    result = _migrate_sqlite_to_aws(source, destination)

    assert result == {"memories": 1, "events": 0, "cursors": 0, "tokens": 0, "conflicts": 0}


def test_migration_copies_a_memory_and_its_local_embedding_when_event_copy_did_not(
    monkeypatch,
):
    row = _memory() | {"state_revision": 1, "state_origin": "local", "state_event_id": ""}
    embedding = np.asarray([0.25, 0.5, 0.75], dtype=np.float32).tobytes()

    class QueryResult:
        def fetchone(self):
            return {"embedding": embedding}

        def fetchall(self):
            return []

    source = type("Source", (), {
        "name": "sqlite", "vault_identity": VaultIdentity(VAULT_ID, "work-vault"),
        "conn": type("Connection", (), {"execute": lambda *_args: QueryResult()})(),
        "export_sync_events": lambda _self, *, after, limit: {"events": [], "cursor": after},
        "iter_memories": lambda _self: [row],
        "read_content": lambda _self, _row: "durable text",
        "list_sync_cursors": lambda _self: [],
        "list_sync_conflicts": lambda _self, *, include_resolved: [],
    })()
    inserted = []
    destination = type("Destination", (), {
        "name": "aws", "vault_identity": source.vault_identity,
        "apply_sync_event": lambda *_args: None,
        "get": lambda *_args: None,
        "insert": lambda _self, record, vector: inserted.append((record, vector.copy())),
        "set_sync_cursor": lambda *_args: None,
        "get_server_token": lambda *_args: None,
        "create_server_token": lambda *_args: None,
        "_table": type("Table", (), {"put_item": lambda *_args, **_kwargs: None})(),
        "_memory_items": lambda _self: [{
            "key": row["key"], "content_hash": row["content_hash"],
        }],
        "vec_status": lambda _self: {"vec_in_sync": True},
    })()

    result = _migrate_sqlite_to_aws(source, destination)

    assert result["memories"] == 1
    assert len(inserted) == 1
    assert inserted[0][0]["content"] == "durable text"
    assert inserted[0][0]["state_event_id"] == ""
    assert inserted[0][1].tolist() == [0.25, 0.5, 0.75]


def test_remote_tombstone_commit_updates_dedupe_feed_and_vector_state(tmp_path):
    aws = FakeAws()
    vault = _open(tmp_path, aws)
    key = "mem_agent-a_removed_1"
    aws.vectors.vectors[key] = {"key": key, "data": {"float32": [1, 0, 0]}}
    event = {
        "event_id": "peer:2", "kind": "tombstone", "key": key,
        "state_revision": 2, "state_origin": "peer", "state_event_id": "peer:2",
    }

    vault._commit_remote_events([event])

    tombstone = next(item for item in aws.items.values() if item.get("recordType") == "tombstone")
    assert tombstone["key"] == key
    assert tombstone["state_revision"] == 2
    assert key not in aws.vectors.vectors
    assert aws.items[(f"VAULT#{VAULT_ID}#META", "SYNC_COUNTER")]["seq"] == 1
    vault.close()


def test_remote_commit_deduplicates_cursors_and_conflicts_idempotently(tmp_path):
    aws = FakeAws()
    vault = _open(tmp_path, aws)
    cursors = [
        {"peer": "peer", "direction": "pull", "token_id": "token", "cursor": 3},
        {"peer": "peer", "direction": "pull", "token_id": "token", "cursor": 8},
    ]
    conflicts = [
        {"base_key": "base", "winner": "old"},
        {"base_key": "base", "winner": "new"},
        {"winner": "ignored"},
    ]

    vault._commit_remote_events([], cursors, conflicts)

    saved = vault.get_sync_cursor("peer", "pull", "token")
    row = aws.items[(f"VAULT#{VAULT_ID}#CONFLICTS", "BASE#base")]
    assert saved == 8
    assert row["winner"] == "new"
    assert len(aws.transactions[-1]) == 2

    vault._commit_remote_events([], cursors, conflicts[:2])
    assert len(aws.transactions[-1]) == 1  # unchanged conflict is not rewritten
    vault.close()


@pytest.mark.parametrize(
    "events,message",
    [
        ([{"event_id": "missing-key"}], "missing its memory key"),
        ([{"event_id": "missing-row", "key": "mem_missing_1", "kind": "snapshot"}],
         "references a missing memory row"),
    ],
)
def test_remote_commit_rejects_events_that_cannot_be_materialized(tmp_path, events, message):
    vault = _open(tmp_path, FakeAws())

    with pytest.raises(ValueError, match=message):
        vault._commit_remote_events(events)

    assert not vault._ddb.transactions
    vault.close()


def test_remote_transaction_limits_cover_action_item_and_total_size_guards(tmp_path):
    vault = _open(tmp_path, FakeAws())
    with pytest.raises(ValueError, match="action limit"):
        vault._check_remote_transaction_limits([
            {"Put": {"Item": {"v": {"S": "x"}}}} for _ in range(101)
        ])
    with pytest.raises(ValueError, match="item-size limit"):
        vault._check_remote_transaction_limits([
            {"Put": {"Item": {"v": {"S": "x" * (400 * 1024)}}}},
        ])
    with pytest.raises(ValueError, match="DynamoDB size limit"):
        vault._check_remote_transaction_limits([
            {"Put": {"Item": {"v": {"S": "x" * 390_000}}}} for _ in range(11)
        ])
    vault.close()


def test_vector_filter_includes_supported_filters_and_expiry_only_when_requested(tmp_path):
    vault = _open(tmp_path, FakeAws())

    assert vault._vector_filter("active", {"unknown": "ignored"}, None) == {"status": "active"}
    assert vault._vector_filter(
        "archived", {"team_id": "team", "memory_type": "semantic", "unknown": "ignored"}, 100,
    ) == {
        "$and": [
            {"status": "archived"}, {"team_id": "team"}, {"memory_type": "semantic"},
            {"$or": [{"expires_at": {"$exists": False}}, {"expires_at": {"$gt": 100}}]},
        ],
    }
    vault.close()


def test_knn_uses_paged_vector_results_and_filters_invalid_candidates(tmp_path, monkeypatch):
    aws = FakeAws()
    vault = _open(tmp_path, aws)
    valid = _memory()
    expired = _memory("expired") | {"key": "mem_agent_expired_1", "expires_at": 1}
    other_team = _memory("other team") | {"key": "mem_agent_other_1", "team_id": "other"}
    archived = _memory("archived") | {"key": "mem_agent_archived_1", "status": "archived"}
    rows = {row["key"]: row for row in (valid, expired, other_team, archived)}
    monkeypatch.setattr(vault, "_refresh_cloud_events", lambda: True)
    monkeypatch.setattr(vault, "get", lambda key: rows.get(key))
    aws.vectors.query_pages = [
        {"vectors": [
            {"key": "absent", "distance": 0.01},
            {"key": expired["key"], "distance": 0.02},
            {"key": other_team["key"], "distance": 0.03},
            {"key": archived["key"], "distance": 0.04},
        ], "nextToken": "page-2"},
        {"vectors": [{"key": valid["key"], "distance": 0.25}]},
    ]

    result = vault.knn(np.asarray([1, 0, 0]), 1, filters={"team_id": "team"}, now=2)

    assert [(row["key"], distance) for row, distance in result] == [(valid["key"], 0.25)]
    assert aws.vectors.query_calls[0]["filter"] == vault._vector_filter(
        "active", {"team_id": "team"}, 2,
    )
    assert aws.vectors.query_calls[1]["nextToken"] == "page-2"
    vault.close()


def test_knn_handles_empty_queries_and_uses_local_fallback_when_vectors_are_unavailable(
    tmp_path, monkeypatch,
):
    vault = _open(tmp_path, FakeAws())
    vector = np.asarray([1, 0, 0], dtype=np.float32)
    vault._cache.insert(_memory(), vector)
    monkeypatch.setattr(vault, "_refresh_cloud_events", lambda: True)

    assert vault.knn(vector, 0) == []
    assert vault.knn(np.zeros(3, dtype=np.float32), 1) == []
    vault._stale = True
    stale = vault.knn(vector, 1)
    assert stale and stale[0][0]["storage_stale"] is True
    vault._stale = False
    vault._vector_projection_ok = False
    fallback = vault.knn(vector, 1)
    assert fallback and fallback[0][0]["key"] == _memory()["key"]
    assert "storage_stale" not in fallback[0][0].keys()
    vault.close()


def test_sweep_orphan_docs_preserves_referenced_content_and_removes_orphans(tmp_path, monkeypatch):
    aws = FakeAws()
    vault = _open(tmp_path, aws)
    content_prefix = f"{VAULT_ID}/content/"
    event_prefix = f"{VAULT_ID}/events/content/"
    referenced = content_prefix + "kept"
    event_referenced = event_prefix + "kept"
    orphan = content_prefix + "orphan"
    event_orphan = event_prefix + "orphan"
    aws.s3.objects.update({key: b"body" for key in (referenced, event_referenced, orphan, event_orphan)})
    monkeypatch.setattr(vault, "_memory_items", lambda: [{"contentObjectKey": referenced}])
    monkeypatch.setattr(
        vault, "export_sync_events",
        lambda *, after, limit: {
            "events": [{"contentObjectKey": event_referenced}], "cursor": 1,
        },
    )

    removed = vault.sweep_orphan_docs()

    assert removed == 2
    assert set(aws.s3.objects) == {referenced, event_referenced}
    vault.close()


def test_delete_object_versions_follows_version_markers_and_filters_other_keys(tmp_path, monkeypatch):
    aws = FakeAws()
    vault = _open(tmp_path, aws)
    key = f"{VAULT_ID}/content/object"
    pages = [
        {
            "Versions": [{"Key": key, "VersionId": "v1"}, {"Key": key + "/child", "VersionId": "v0"}],
            "DeleteMarkers": [{"Key": key, "VersionId": "marker"}],
            "IsTruncated": True, "NextKeyMarker": key, "NextVersionIdMarker": "v1",
        },
        {"Versions": [{"Key": key, "VersionId": "v2"}], "IsTruncated": False},
    ]
    calls = []
    monkeypatch.setattr(
        aws.s3, "list_object_versions",
        lambda **kwargs: calls.append(kwargs) or pages.pop(0),
    )

    vault._delete_object_versions(key)

    assert calls[1]["KeyMarker"] == key
    assert calls[1]["VersionIdMarker"] == "v1"
    assert [entry["VersionId"] for batch in aws.s3.deleted_batches for entry in batch] == [
        "v1", "marker", "v2",
    ]
    vault.close()
