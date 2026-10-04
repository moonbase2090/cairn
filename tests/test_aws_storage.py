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
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)
        return {}


class FakeVectors:
    def __init__(self, aws):
        self.aws = aws
        self.vectors: dict[str, dict] = {}

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
        tmp_path, "mock-embedder", 3, _config(),
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
