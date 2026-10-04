from __future__ import annotations

import importlib.util
from pathlib import Path

from boto3.dynamodb.conditions import Key


_PATH = Path(__file__).resolve().parents[1] / "aws" / "infra" / "lambda" / "cleanup.py"
_SPEC = importlib.util.spec_from_file_location("cairn_aws_cleanup", _PATH)
cleanup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cleanup)

VAULT_ID = "0123456789abcdef0123456789abcdef"
OBJECT_KEY = f"{VAULT_ID}/content/abc123"


class FakeTable:
    def __init__(self, tombstone, memory=None):
        self.tombstone = tombstone
        self.memory = memory
        self.updates = []

    def query(self, **kwargs):
        condition = kwargs["KeyConditionExpression"].get_expression()
        key = condition["values"][0].name
        value = condition["values"][1]
        if key == "GSI0PK" and value == f"VAULT#{VAULT_ID}#TOMBSTONE":
            return {"Items": [self.tombstone]}
        if key == "PK" and self.memory and self.memory["PK"] == value:
            return {"Items": [self.memory]}
        return {"Items": []}

    def update_item(self, *, Key, UpdateExpression, **_kwargs):
        self.updates.append((Key, UpdateExpression))
        if "vectorCleanupDone" in UpdateExpression:
            self.tombstone["vectorCleanupDone"] = True
        if "contentCleanupDone" in UpdateExpression:
            self.tombstone["contentCleanupDone"] = True


class FakeDynamo:
    def __init__(self, table):
        self.table = table

    def Table(self, _name):
        return self.table


class FakeS3:
    def __init__(self):
        self.deleted = []

    def list_object_versions(self, *, Prefix, **_kwargs):
        return {
            "Versions": [{"Key": Prefix, "VersionId": "version-1"}],
            "DeleteMarkers": [],
            "IsTruncated": False,
        }

    def delete_objects(self, *, Delete, **_kwargs):
        self.deleted.extend(Delete["Objects"])
        return {}


class FakeVectors:
    def __init__(self):
        self.deleted = []

    def delete_vectors(self, *, keys, **_kwargs):
        self.deleted.extend(keys)


def _table(memory=None):
    tombstone = {
        "PK": f"VAULT#{VAULT_ID}#TOMBSTONE#00",
        "SK": "TOMBSTONE#mem_agent_old",
        "key": "mem_agent_old",
        "content_hash": "sha256:abc123",
        "contentObjectKey": OBJECT_KEY,
    }
    return FakeTable(tombstone, memory)


def _run(monkeypatch, table):
    s3 = FakeS3()
    vectors = FakeVectors()
    monkeypatch.setattr(cleanup.boto3, "resource", lambda _name: FakeDynamo(table))
    monkeypatch.setattr(
        cleanup.boto3, "client",
        lambda name: s3 if name == "s3" else vectors,
    )
    monkeypatch.setenv("MEMORY_TABLE", "table")
    monkeypatch.setenv("CONTENT_BUCKET", "bucket")
    monkeypatch.setenv("VECTOR_BUCKET", "vectors")
    monkeypatch.setenv("VECTOR_INDEX", "index")
    monkeypatch.setenv("VAULT_ID", VAULT_ID)
    result = cleanup.handler({}, None)
    return result, s3, vectors


def test_cleanup_removes_unreferenced_versions_and_vector_idempotently(monkeypatch):
    table = _table()

    result, s3, vectors = _run(monkeypatch, table)
    again, _, _ = _run(monkeypatch, table)

    assert result == {
        "processed": 1,
        "removed_content_objects": 1,
        "removed_vectors": 1,
        "vault_id": VAULT_ID,
    }
    assert s3.deleted == [{"Key": OBJECT_KEY, "VersionId": "version-1"}]
    assert vectors.deleted == ["mem_agent_old"]
    assert again["processed"] == 0


def test_cleanup_keeps_content_while_another_memory_owns_the_hash(monkeypatch):
    memory = {
        "PK": f"VAULT#{VAULT_ID}#MEMORY#00",
        "SK": "MEMORY#mem_agent_live",
        "recordType": "memory",
        "content_hash": "sha256:abc123",
    }
    table = _table(memory)

    result, s3, vectors = _run(monkeypatch, table)

    assert result["removed_content_objects"] == 0
    assert not s3.deleted
    assert vectors.deleted == ["mem_agent_old"]
    assert table.tombstone["vectorCleanupDone"] is True
    assert "contentCleanupDone" not in table.tombstone
