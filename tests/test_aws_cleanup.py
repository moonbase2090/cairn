from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

from botocore.exceptions import ClientError
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
        self.lease_owner = None
        self.lease_expires_at = None
        self.lease_lock = threading.Lock()
        self.query_entered = None
        self.allow_query = None
        self.block_first_query = False

    def query(self, **kwargs):
        condition = kwargs["KeyConditionExpression"].get_expression()
        key = condition["values"][0].name
        value = condition["values"][1]
        if key == "GSI0PK" and value == f"VAULT#{VAULT_ID}#TOMBSTONE":
            if self.query_entered is not None and self.block_first_query:
                self.block_first_query = False
                self.query_entered.set()
                self.allow_query.wait(timeout=5)
            return {"Items": [self.tombstone]}
        if key == "PK" and self.memory and self.memory["PK"] == value:
            return {"Items": [self.memory]}
        return {"Items": []}

    def update_item(self, *, Key, UpdateExpression, ConditionExpression=None,
                    ExpressionAttributeValues=None, **_kwargs):
        if Key["PK"].endswith("#CLEANUP#LOCK"):
            prefix = "SET "
            assert UpdateExpression.startswith(prefix)
            assignments = {
                assignment.split("=", 1)[0].strip(): assignment.split("=", 1)[1].strip()
                for assignment in UpdateExpression[len(prefix):].split(",")
            }
            assert assignments == {
                "leaseOwner": ":owner",
                "leaseExpiresAt": ":expires",
            }
            with self.lease_lock:
                if not self._lease_condition_holds(
                    ConditionExpression, ExpressionAttributeValues,
                ):
                    raise _conditional_failure()
                self.lease_owner = ExpressionAttributeValues[":owner"]
                self.lease_expires_at = ExpressionAttributeValues[":expires"]
            return
        self.updates.append((Key, UpdateExpression))
        if "vectorCleanupDone" in UpdateExpression:
            self.tombstone["vectorCleanupDone"] = True
        if "contentCleanupDone" in UpdateExpression:
            self.tombstone["contentCleanupDone"] = True

    def _lease_condition_holds(self, expression, values):
        prefix = "attribute_not_exists(leaseExpiresAt) OR leaseExpiresAt "
        assert expression.startswith(prefix)
        operator, reference = expression[len(prefix):].split()
        assert operator in {"<", ">"}
        assert reference == ":now"
        if self.lease_expires_at is None:
            return True
        if operator == "<":
            return self.lease_expires_at < values[reference]
        return self.lease_expires_at > values[reference]

    def delete_item(self, *, Key, ConditionExpression, ExpressionAttributeValues):
        assert Key["PK"].endswith("#CLEANUP#LOCK")
        assert ConditionExpression == "leaseOwner = :owner"
        with self.lease_lock:
            if self.lease_owner != ExpressionAttributeValues[":owner"]:
                raise _conditional_failure()
            self.lease_owner = None
            self.lease_expires_at = None


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


def _conditional_failure():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "condition failed"}},
        "UpdateItem",
    )


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
    assert table.lease_owner is None


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


def test_overlapping_cleanup_invocations_share_a_lease(monkeypatch):
    table = _table()
    table.query_entered = threading.Event()
    table.allow_query = threading.Event()
    table.block_first_query = True
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
    first_result = []
    first_errors = []
    first_done = threading.Event()

    def run_first():
        try:
            first_result.append(cleanup.handler({}, None))
        except Exception as error:
            first_errors.append(error)
        finally:
            first_done.set()

    first = threading.Thread(target=run_first)

    first.start()
    while not table.query_entered.is_set() and not first_done.wait(timeout=0.01):
        pass
    assert table.query_entered.is_set(), first_errors
    second = cleanup.handler({}, None)
    table.allow_query.set()
    first.join(timeout=5)

    assert not first.is_alive()
    assert first_result[0]["processed"] == 1
    assert second == {"processed": 0, "skipped": "lease-held", "vault_id": VAULT_ID}
    assert len(s3.deleted) == 1
    assert vectors.deleted == ["mem_agent_old"]
    assert table.lease_owner is None
