"""Direct AWS data plane with a complete local SQLite read and FTS cache.

The AWS table is authoritative. A local transaction commits only after the
DynamoDB row and event transaction succeeds; writes fail while AWS is offline.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any
import uuid

import numpy as np

from .models import content_digest
from .storage import (
    MEMORY_FIELDS,
    ContentIntegrityError,
    MemoryQuery,
    SpaceMismatchError,
    StorageBackend,
    StorageConfig,
)
from .vault_identity import VaultIdentity, initial_vault_name
from .vault_identity import safe_vault_name


_EVENT_SHARDS = 16
_DYNAMODB_ACTION_LIMIT = 100
_DYNAMODB_TRANSACTION_BYTES = 4 * 1024 * 1024
_EMBEDDING_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _shard(text: str, count: int = _EVENT_SHARDS) -> str:
    return f"{int(_sha(text)[:8], 16) % count:02x}"


def _dynamo_value(value):
    """Convert Python floats to DynamoDB's required Decimal values."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _dynamo_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dynamo_value(item) for item in value]
    if isinstance(value, np.generic):
        return _dynamo_value(value.item())
    return value


def _attribute_value_size(value: dict) -> int:
    kind, raw = next(iter(value.items()))
    if kind == "S":
        return len(raw.encode("utf-8"))
    if kind == "N":
        return (len(raw) + 1) // 2 + 1
    if kind == "B":
        return len(raw)
    if kind in {"BOOL", "NULL"}:
        return 1
    if kind == "M":
        return 3 + _item_size(raw)
    if kind == "L":
        return 3 + sum(_attribute_value_size(item) for item in raw)
    if kind in {"SS", "NS", "BS"}:
        return 3 + sum(
            len(item.encode("utf-8")) if isinstance(item, str) else len(item)
            for item in raw
        )
    raise ValueError("AWS item uses an unsupported DynamoDB value type")


def _item_size(item: dict) -> int:
    return sum(
        len(name.encode("utf-8")) + _attribute_value_size(value)
        for name, value in item.items()
    )


class AwsVault(StorageBackend):
    """A Cairn vault backed by DynamoDB, S3, and S3 Vectors."""

    name = "aws"

    def __init__(
        self,
        vault_dir: Path,
        embed_name: str,
        dims: int,
        config: StorageConfig,
        create: bool = False,
        doc_threshold: int | None = None,
        *,
        _session=None,
        _key_factory=None,
        _serializer=None,
    ) -> None:
        del create  # Cloud resources are created only by the reviewed CDK control plane.
        if config.backend != "aws":
            raise ValueError("AwsVault requires backend = 'aws'")
        if not config.vault_id:
            raise ValueError("AWS storage needs a logical vault ID")
        self._config = config
        self._vault_dir = Path(vault_dir)
        self._identity = VaultIdentity(config.vault_id, config.vault_name or initial_vault_name(self._vault_dir))
        self._stale = False
        self._vector_projection_ok = True
        self._vector_error: str | None = None
        self._tx_depth = 0
        self._pending_cursors: list[dict] = []

        if _session is None:
            try:
                import boto3
            except ImportError as error:
                raise ImportError(
                    "AWS storage requires the optional dependency; install Cairn with `pip install 'cairn[aws]'`"
                ) from error
            _session = boto3.session.Session(
                profile_name=config.profile,
                region_name=config.region,
            )
        self._session = _session
        self._table = _session.resource("dynamodb").Table(config.table)
        self._cache_table = _session.resource("dynamodb").Table(config.cache_table)
        self._s3 = _session.client("s3")
        self._vectors = _session.client("s3vectors")
        self._ddb = _session.client("dynamodb")
        if _key_factory is None or _serializer is None:
            try:
                from boto3.dynamodb.conditions import Key
                from boto3.dynamodb.types import TypeSerializer
            except ImportError as error:
                raise ImportError(
                    "AWS storage requires the optional dependency; install Cairn with `pip install 'cairn[aws]'`"
                ) from error
            _key_factory = _key_factory or Key
            _serializer = _serializer or TypeSerializer()
        self._key = _key_factory
        self._serializer = _serializer

        cache_dir = self._vault_dir / "aws-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        from .store import Vault

        self._cache = Vault(cache_dir / "vault.db", embed_name, dims,
                            create=True, doc_threshold=doc_threshold)
        self._set_cache_identity()
        self._check_local_identity()
        self._load_cloud_snapshot()

    @property
    def vault_dir(self) -> Path:
        return self._vault_dir

    @property
    def vault_identity(self) -> VaultIdentity:
        return self._identity

    @property
    def doc_threshold(self) -> int:
        return self._cache.doc_threshold

    def _set_cache_identity(self) -> None:
        self._cache._set_meta("vault_id", self._identity.vault_id)
        self._cache._set_meta("vault_name", self._identity.name)
        self._cache._set_meta("vault_identity_migration", "1")
        self._cache._vault_identity = self._identity

    def _check_local_identity(self) -> None:
        db_path = self._vault_dir / "vault.db"
        if not db_path.exists():
            return
        try:
            with sqlite3.connect(db_path) as connection:
                rows = dict(connection.execute(
                    "SELECT k, v FROM meta WHERE k IN ('vault_id', 'vault_name')",
                ).fetchall())
        except sqlite3.Error as error:
            raise SpaceMismatchError("local vault identity could not be verified") from error
        if not rows.get("vault_id") or not rows.get("vault_name"):
            raise SpaceMismatchError("local vault identity is missing; refusing to open AWS storage")
        if rows["vault_id"] != self._identity.vault_id:
            raise SpaceMismatchError("configured AWS vault ID does not match the local vault identity")
        if safe_vault_name(rows["vault_name"]) != rows["vault_name"]:
            raise SpaceMismatchError("local vault display name is unsafe; refusing to open AWS storage")
        if rows["vault_name"] != self._identity.name:
            raise SpaceMismatchError("configured AWS vault name does not match the local vault identity")

    def _memory_partition(self) -> str:
        return f"VAULT#{self._identity.vault_id}#MEMORY"

    def _event_counter_key(self) -> dict[str, str]:
        return {"PK": f"VAULT#{self._identity.vault_id}#META", "SK": "SYNC_COUNTER"}

    def _event_marker_key(self, event_id: str) -> dict[str, str]:
        return {
            "PK": f"VAULT#{self._identity.vault_id}#EVENTID#{_shard(event_id)}",
            "SK": f"EVENT#{event_id}",
        }

    def _memory_key(self, key: str) -> dict[str, str]:
        return {
            "PK": f"VAULT#{self._identity.vault_id}#MEMORY#{_shard(key)}",
            "SK": f"MEMORY#{key}",
        }

    def _tombstone_key(self, key: str) -> dict[str, str]:
        return {
            "PK": f"VAULT#{self._identity.vault_id}#TOMBSTONE#{_shard(key)}",
            "SK": f"TOMBSTONE#{key}",
        }

    def _event_partition(self, key: str) -> str:
        return f"VAULT#{self._identity.vault_id}#EVENTS#{_shard(key)}"

    def _content_key(self, content_hash: str) -> str:
        digest = content_hash.removeprefix("sha256:")
        return f"{self._identity.vault_id}/content/{digest}"

    def _event_content_key(self, content_hash: str) -> str:
        digest = content_hash.removeprefix("sha256:")
        return f"{self._identity.vault_id}/events/content/{digest}"

    def _query_pages(self, **kwargs) -> Iterator[dict]:
        request = dict(kwargs)
        while True:
            page = self._table.query(**request)
            yield page
            last = page.get("LastEvaluatedKey")
            if not last:
                break
            request["ExclusiveStartKey"] = last

    def _memory_items(self) -> list[dict]:
        items = []
        for shard in range(_EVENT_SHARDS):
            partition = f"{self._memory_partition()}#{shard:02x}"
            for page in self._query_pages(
                KeyConditionExpression=self._key("PK").eq(partition),
                ConsistentRead=True,
            ):
                items.extend(
                    item for item in page.get("Items", [])
                    if item.get("recordType") == "memory"
                )
        return items

    def _get_counter(self) -> int:
        response = self._table.get_item(Key=self._event_counter_key(), ConsistentRead=True)
        return int(response.get("Item", {}).get("seq", 0))

    def _load_cloud_snapshot(self) -> None:
        items = self._memory_items()
        with self._cache.transaction():
            self._cache.conn.execute("DELETE FROM memories")
            self._cache.conn.execute("DELETE FROM sync_events")
            self._cache.conn.execute("DELETE FROM sync_tombstones")
            self._cache.conn.execute("DELETE FROM sync_conflicts")
            for item in items:
                row = self._decode_row(item)
                content = self.read_content(row)
                from .secret_scan import scan_content

                scan_content(content)
                vector = self._row_vector(item)
                rec = {field: row.get(field) for field in MEMORY_FIELDS}
                rec["content"] = content
                rec["content_hash"] = row.get("content_hash")
                self._cache.insert(rec, vector)
            self._cache.conn.execute("DELETE FROM sync_events")
            self._cache.conn.execute("DELETE FROM sync_tombstones")
            self._cache.conn.execute("DELETE FROM sync_conflicts")
            self._cache._set_meta("aws_sync_cursor", str(self._get_counter()))
        self._vector_projection_ok = all(item.get("vector_projected", False) for item in items)

    def _refresh_cloud_events(self) -> bool:
        """Apply durable remote events to the local FTS cache; reads can stay stale offline."""
        try:
            raw_cursor = self._cache._get_meta("aws_sync_cursor") or "0"
            cursor = int(raw_cursor)
            while True:
                pack = self.export_sync_events(after=cursor, limit=1000)
                events = pack["events"]
                if not events:
                    self._cache._set_meta("aws_sync_cursor", str(pack["cursor"]))
                    self._stale = False
                    return True
                for event in events:
                    for text in _event_contents(event):
                        from .secret_scan import scan_content

                        scan_content(text)
                with self._cache.transaction():
                    for event in events:
                        self._cache.apply_sync_event(event)
                cursor = int(pack["cursor"])
                self._cache._set_meta("aws_sync_cursor", str(cursor))
                self._stale = False
                if len(events) < 1000:
                    return True
        except Exception as error:
            from .secret_scan import SecretAdmissionError

            if isinstance(error, SecretAdmissionError):
                raise
            self._stale = True
            return False

    def _ensure_online_for_write(self) -> None:
        if not self._refresh_cloud_events():
            raise OSError("AWS is unavailable; writes are disabled while the local cache is offline")

    @contextmanager
    def transaction(self):
        outer = self._tx_depth == 0
        if outer:
            self._ensure_online_for_write()
            start = int(self._cache.export_sync_events().get("cursor", 0))
            self._pending_cursors = []
        self._tx_depth += 1
        try:
            with self._cache.transaction():
                yield self
                if outer:
                    feed = self._cache.export_sync_events(after=start, limit=100_000)
                    conflicts = self._cache.list_sync_conflicts(include_resolved=True)
                    self._commit_remote_events(
                        feed["events"], self._pending_cursors, conflicts,
                    )
        finally:
            self._tx_depth -= 1
            if outer:
                self._pending_cursors = []

    def _serialize(self, values: dict) -> dict:
        return {
            name: self._serializer.serialize(_dynamo_value(value))
            for name, value in values.items()
        }

    def _table_item(self, item: dict) -> dict:
        return {"TableName": self._config.table, "Item": self._serialize(item)}

    def _content_object(self, content_hash: str, content: str) -> str:
        key = self._content_key(content_hash)
        self._put_content_object(key, content)
        return key

    def _put_content_object(self, key: str, content: str) -> None:
        body = content.encode("utf-8")
        try:
            self._s3.head_object(Bucket=self._config.content_bucket, Key=key)
        except Exception as error:
            status = getattr(error, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 404:
                raise OSError("AWS content storage could not be checked") from None
            self._s3.put_object(
                Bucket=self._config.content_bucket,
                Key=key,
                Body=body,
                ContentType="text/plain; charset=utf-8",
            )

    def _get_content_object(self, key: str, content_hash: str) -> str:
        try:
            response = self._s3.get_object(Bucket=self._config.content_bucket, Key=key)
            content = response["Body"].read().decode("utf-8")
        except Exception:
            raise ContentIntegrityError("stored content document is missing or unreadable") from None
        if f"sha256:{content_digest(content)}" != content_hash:
            raise ContentIntegrityError("stored content document failed its integrity check")
        return content

    def _row_vector(self, item: dict) -> np.ndarray:
        raw = item.get("embedding")
        if raw is not None:
            return np.frombuffer(bytes(raw), dtype=np.float32).copy()
        response = self._vectors.get_vectors(
            vectorBucketName=self._config.vector_bucket,
            indexName=self._config.vector_index,
            keys=[item["key"]],
            returnData=True,
        )
        found = response.get("vectors", [])
        if not found:
            raise ContentIntegrityError("stored vector is missing")
        return np.asarray(found[0]["data"]["float32"], dtype=np.float32)

    def _decode_row(self, item: dict) -> Mapping:
        values = {field: item.get(field) for field in MEMORY_FIELDS}
        values["content_ref"] = item.get("contentObjectKey") if values.get("content") is None else None
        values["contentObjectKey"] = item.get("contentObjectKey")
        return MappingProxyType(values)

    def _memory_item(self, row: Mapping, vector: bytes, content_key: str) -> dict:
        key = str(row["key"])
        created_at = int(row.get("created_at") or 0)
        item = {
            **self._memory_key(key),
            "recordType": "memory",
            "vault_id": self._identity.vault_id,
            "GSI0PK": self._memory_partition(),
            "GSI0SK": f"MEMORY#{created_at:020d}#{key}",
            "GSI1PK": f"VAULT#{self._identity.vault_id}#CANONICAL#{row.get('canonical_id') or ''}",
            "GSI1SK": f"CREATED#{created_at:020d}#{key}",
            "GSI2PK": f"VAULT#{self._identity.vault_id}#TASK#{row.get('task_id') or ''}",
            "GSI2SK": f"CREATED#{created_at:020d}#{key}",
            "GSI3PK": f"VAULT#{self._identity.vault_id}#HASH#{row.get('content_hash') or ''}",
            "GSI3SK": f"MEMORY#{key}",
            "contentObjectKey": content_key,
            "embedding": bytes(vector),
            "vector_projected": False,
        }
        item.update({field: row.get(field) for field in MEMORY_FIELDS})
        content = row.get("content")
        if content is not None and len(str(content).encode("utf-8")) > self.doc_threshold:
            item["content"] = None
        return item

    def _vector_metadata(self, row: Mapping) -> dict[str, Any]:
        metadata: dict[str, Any] = {"status": str(row.get("status") or "active")}
        for field in ("canonical_id", "task_id", "team_id", "memory_type", "agent_id"):
            value = row.get(field)
            if isinstance(value, (str, int, float, bool)) and value != "":
                metadata[field] = value
        expires = row.get("expires_at")
        if isinstance(expires, (int, float)):
            metadata["expires_at"] = int(expires)
        if len(json.dumps(metadata, separators=(",", ":")).encode("utf-8")) > 2048:
            raise ValueError("vector filter metadata exceeds the AWS S3 Vectors filterable metadata limit")
        return metadata

    def _project_vector(self, row: Mapping, vector: bytes) -> None:
        arr = np.frombuffer(vector, dtype=np.float32)
        self._vectors.put_vectors(
            vectorBucketName=self._config.vector_bucket,
            indexName=self._config.vector_index,
            vectors=[{
                "key": str(row["key"]),
                "data": {"float32": arr.tolist()},
                "metadata": self._vector_metadata(row),
            }],
        )

    def _write_embedding_cache(self, row: Mapping, vector: bytes) -> None:
        content_hash = str(row.get("content_hash") or "")
        if content_hash:
            self.put_embedding_cache(
                content_hash, self._cache._embed_name,
                np.frombuffer(vector, dtype=np.float32),
            )

    def put_embedding_cache(self, content_hash: str, embedder: str,
                            vector: np.ndarray) -> None:
        if embedder != self._cache._embed_name:
            return
        encoded = np.asarray(vector, dtype=np.float32)
        if encoded.shape != (self._cache._dims,):
            raise ValueError("embedding cache vector has unexpected dimensions")
        now = int(time.time())
        self._cache_table.put_item(Item={
            "PK": f"VAULT#{self._identity.vault_id}#EMBED#{_shard(content_hash)}",
            "SK": f"HASH#{content_hash}#MODEL#{embedder}",
            "vault_id": self._identity.vault_id,
            "content_hash": content_hash,
            "embedder": embedder,
            "embedding": encoded.tobytes(),
            "created_at": now,
            "ttlEpoch": now + _EMBEDDING_CACHE_TTL_SECONDS,
        })

    def get_embedding_cache(self, content_hash: str, embedder: str) -> np.ndarray | None:
        if embedder != self._cache._embed_name:
            return None
        response = self._cache_table.get_item(Key={
            "PK": f"VAULT#{self._identity.vault_id}#EMBED#{_shard(content_hash)}",
            "SK": f"HASH#{content_hash}#MODEL#{embedder}",
        })
        item = response.get("Item")
        if not item or int(item.get("ttlEpoch", 0)) <= int(time.time()):
            return None
        return np.frombuffer(bytes(item["embedding"]), dtype=np.float32).copy()

    def _remote_event_rows(self, events: list[dict]) -> dict[str, tuple[dict, Mapping | None]]:
        latest: dict[str, tuple[dict, Mapping | None]] = {}
        for event in events:
            key = event.get("key")
            if not isinstance(key, str):
                raise ValueError("AWS sync event is missing its memory key")
            cached = self._cache.get(key)
            latest[key] = (event, dict(cached) if cached is not None else None)
        return latest

    def _remote_event_actions(self, events: list[dict], counter: int) -> list[dict]:
        actions: list[dict] = []
        for offset, event in enumerate(events, start=1):
            seq = counter + offset
            event_payload = dict(event)
            event_payload.pop("feed_seq", None)
            content_object_key = None
            snapshot = event_payload.get("snapshot")
            if isinstance(snapshot, dict):
                content = snapshot.get("content")
                content_hash = snapshot.get("content_hash")
                if isinstance(content, str) and isinstance(content_hash, str):
                    self._content_object(content_hash, content)
                    content_object_key = self._event_content_key(content_hash)
                    self._put_content_object(content_object_key, content)
                if content_object_key:
                    snapshot.pop("content", None)
                    event_payload["contentObjectKey"] = content_object_key
            encoded_event = json.dumps(event_payload, separators=(",", ":"), default=str)
            key = str(event["key"])
            event_item = {
                "PK": self._event_partition(key),
                "SK": f"SEQ#{seq:020d}",
                "recordType": "event",
                "vault_id": self._identity.vault_id,
                "event_id": event["event_id"],
                "seq": seq,
                "key": event["key"],
                "GSI5PK": f"VAULT#{self._identity.vault_id}#EVENT#{event['event_id']}",
                "GSI5SK": "EVENT",
                "contentObjectKey": content_object_key,
                "event_json": encoded_event,
            }
            actions.append({"Put": {
                **self._table_item(event_item),
                "ConditionExpression": "attribute_not_exists(PK)",
            }})
            marker = self._event_marker_key(str(event["event_id"]))
            actions.append({"Put": {
                **self._table_item({
                    **marker,
                    "recordType": "event-dedupe",
                    "vault_id": self._identity.vault_id,
                    "event_id": event["event_id"],
                }),
                "ConditionExpression": "attribute_not_exists(PK)",
            }})
        return actions

    def _remote_tombstone_actions(self, key: str, event: dict) -> list[dict]:
        existing = self._table.get_item(Key=self._memory_key(key), ConsistentRead=True).get("Item")
        old_tombstone = self._table.get_item(
            Key=self._tombstone_key(key), ConsistentRead=True,
        ).get("Item")
        revision = int(event.get("state_revision", 0))
        origin = str(event.get("state_origin", event.get("origin_id", "")))
        event_id = str(event.get("state_event_id", event["event_id"]))
        condition = (
            "attribute_not_exists(PK) OR #revision < :revision OR "
            "(#revision = :revision AND #origin < :origin) OR "
            "(#revision = :revision AND #origin = :origin AND #event <= :event)"
        )
        names = {"#revision": "state_revision", "#origin": "state_origin",
                 "#event": "state_event_id"}
        values = self._serialize({":revision": revision, ":origin": origin, ":event": event_id})
        tombstone = {
            **self._tombstone_key(key),
            "recordType": "tombstone",
            "vault_id": self._identity.vault_id,
            "key": key,
            "event_id": event["event_id"],
            "state_revision": event.get("state_revision", 0),
            "state_origin": event.get("state_origin", event.get("origin_id", "")),
            "state_event_id": event.get("state_event_id", event["event_id"]),
            "content_hash": (existing or old_tombstone or {}).get("content_hash"),
            "contentObjectKey": (existing or old_tombstone or {}).get("contentObjectKey"),
            "GSI0PK": f"VAULT#{self._identity.vault_id}#TOMBSTONE",
            "GSI0SK": f"TOMBSTONE#{key}",
        }
        return [
            {"Delete": {
                "TableName": self._config.table,
                "Key": self._serialize(self._memory_key(key)),
                "ConditionExpression": condition,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": values,
            }},
            {"Put": {
                **self._table_item(tombstone),
                "ConditionExpression": condition,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": values,
            }},
        ]

    def _remote_memory_action(self, key: str, event: dict, row: Mapping) -> tuple[dict, tuple[dict, Mapping, bytes]]:
        full_content = self._cache.read_content(row)
        content_hash = str(row["content_hash"])
        content_key = self._content_object(content_hash, full_content)
        cache_row = self._cache.conn.execute(
            "SELECT embedding FROM memories WHERE key=?", (key,),
        ).fetchone()
        vector = bytes(cache_row["embedding"])
        item = self._memory_item(row, vector, content_key)
        action = {"Put": {
            **self._table_item(item),
            "ConditionExpression": (
                "attribute_not_exists(PK) OR #revision < :revision OR "
                "(#revision = :revision AND #origin < :origin) OR "
                "(#revision = :revision AND #origin = :origin AND #event < :event)"
            ),
            "ExpressionAttributeNames": {
                "#revision": "state_revision", "#origin": "state_origin",
                "#event": "state_event_id",
            },
            "ExpressionAttributeValues": self._serialize({
                ":revision": int(event.get("state_revision", item.get("state_revision", 1))),
                ":origin": str(event.get("state_origin", event.get("origin_id", ""))),
                ":event": str(event.get("state_event_id", event["event_id"])),
            }),
        }}
        return action, (item, row, vector)

    def _remote_state_actions(
        self, latest: dict[str, tuple[dict, Mapping | None]],
    ) -> tuple[list[dict], list[tuple[dict, Mapping | None, bytes | None]]]:
        actions: list[dict] = []
        vector_updates: list[tuple[dict, Mapping | None, bytes | None]] = []
        for key, (event, row) in latest.items():
            if event.get("kind") == "tombstone":
                actions.extend(self._remote_tombstone_actions(key, event))
                vector_updates.append((dict(event), None, None))
                continue
            if row is None:
                raise ValueError("AWS sync event references a missing memory row")
            action, vector_update = self._remote_memory_action(key, event, row)
            actions.append(action)
            vector_updates.append(vector_update)
        return actions, vector_updates

    def _remote_cursor_actions(self, cursors: list[dict]) -> list[dict]:
        actions = []
        for cursor in cursors:
            item = {
                **self._cursor_key(cursor["peer"], cursor["direction"], cursor["token_id"]),
                "recordType": "sync-cursor",
                "vault_id": self._identity.vault_id,
                **cursor,
            }
            actions.append({"Put": {
                **self._table_item(item),
                "ConditionExpression": "attribute_not_exists(#cursor) OR #cursor <= :cursor",
                "ExpressionAttributeNames": {"#cursor": "cursor"},
                "ExpressionAttributeValues": self._serialize({":cursor": int(cursor["cursor"])}),
            }})
        return actions

    def _remote_conflict_actions(self, conflicts: list[dict] | None) -> list[dict]:
        actions = []
        latest = {
            str(row.get("base_key", "")): row
            for row in (conflicts or []) if row.get("base_key")
        }
        for conflict in latest.values():
            conflict_item = {
                "PK": f"VAULT#{self._identity.vault_id}#CONFLICTS",
                "SK": f"BASE#{conflict['base_key']}",
                "recordType": "sync-conflict",
                "vault_id": self._identity.vault_id,
                **conflict,
            }
            existing = self._table.get_item(
                Key={"PK": conflict_item["PK"], "SK": conflict_item["SK"]},
                ConsistentRead=True,
            ).get("Item")
            changed = any(
                existing is None or existing.get(name) != value
                for name, value in conflict_item.items()
                if name not in {"PK", "SK"}
            )
            if changed:
                actions.append({"Put": self._table_item(conflict_item)})
        return actions

    def _check_remote_transaction_limits(self, actions: list[dict]) -> None:
        if len(actions) > _DYNAMODB_ACTION_LIMIT:
            raise ValueError("AWS transaction exceeds the documented DynamoDB action limit")
        put_items = [action["Put"]["Item"] for action in actions if "Put" in action]
        item_sizes = [_item_size(item) for item in put_items]
        if any(size > 400 * 1024 for size in item_sizes):
            raise ValueError("AWS item exceeds the documented DynamoDB item-size limit")
        if sum(item_sizes) > _DYNAMODB_TRANSACTION_BYTES:
            raise ValueError("AWS transaction exceeds the documented DynamoDB size limit")

    def _apply_remote_vector_updates(
        self, vector_updates: list[tuple[dict, Mapping | None, bytes | None]],
    ) -> None:
        for item, row, vector in vector_updates:
            try:
                if row is None:
                    self._vectors.delete_vectors(
                        vectorBucketName=self._config.vector_bucket,
                        indexName=self._config.vector_index,
                        keys=[str(item["key"])],
                    )
                else:
                    self._project_vector(row, vector or b"")
                    self._table.update_item(
                        Key=self._memory_key(str(row["key"])),
                        UpdateExpression="SET vector_projected = :yes",
                        ExpressionAttributeValues={":yes": True},
                    )
                    self._write_embedding_cache(row, vector or b"")
            except Exception as error:
                self._vector_projection_ok = False
                self._vector_error = type(error).__name__

    def _commit_remote_events(
        self, events: list[dict], cursors: list[dict] | None = None,
        conflicts: list[dict] | None = None,
    ) -> None:
        events = [
            event for event in events
            if not self._event_marker_exists(str(event.get("event_id", "")))
        ]
        cursors_by_peer: dict[tuple[str, str, str], dict] = {}
        for cursor in cursors or []:
            identity = (cursor["peer"], cursor["direction"], cursor["token_id"])
            current = cursors_by_peer.get(identity)
            if current is None or int(cursor["cursor"]) >= int(current["cursor"]):
                cursors_by_peer[identity] = cursor
        cursors = list(cursors_by_peer.values())
        latest = self._remote_event_rows(events)
        counter = self._get_counter() if events else 0
        actions = self._remote_event_actions(events, counter)
        state_actions, vector_updates = self._remote_state_actions(latest)
        actions.extend(state_actions)
        actions.extend(self._remote_cursor_actions(cursors))
        actions.extend(self._remote_conflict_actions(conflicts))
        if events:
            actions.append({"Update": {
                "TableName": self._config.table,
                "Key": self._serialize(self._event_counter_key()),
                "UpdateExpression": "SET #seq = :next",
                "ConditionExpression": "attribute_not_exists(#seq) OR #seq = :previous",
                "ExpressionAttributeNames": {"#seq": "seq"},
                "ExpressionAttributeValues": self._serialize({":next": counter + len(events), ":previous": counter}),
            }})
        if not actions:
            return
        self._check_remote_transaction_limits(actions)
        self._ddb.transact_write_items(
            TransactItems=actions,
            ClientRequestToken=uuid.uuid4().hex,
        )
        self._apply_remote_vector_updates(vector_updates)

    def insert(self, rec: dict, vector: np.ndarray) -> int:
        from .secret_scan import scan_content

        scan_content(rec.get("content"))
        with self.transaction():
            return self._cache.insert(rec, vector)

    def get(self, key: str) -> Mapping | None:
        try:
            item = self._table.get_item(Key=self._memory_key(key), ConsistentRead=True).get("Item")
            self._stale = False
        except Exception:
            self._stale = True
            cached = self._cache.get(key)
            return _mark_stale(cached) if cached else None
        if not item or item.get("recordType") != "memory":
            return None
        return self._decode_row(item)

    def by_hash(self, content_hash: str, task_id: str, status: str = "active") -> list[Mapping]:
        self._refresh_cloud_events()
        return self._cache.by_hash(content_hash, task_id, status)

    def find(self, query: MemoryQuery, limit: int = 100,
             with_embedding: bool = False) -> list[Mapping]:
        self._refresh_cloud_events()
        rows = self._cache.find(query, limit, with_embedding=with_embedding)
        return [_mark_stale(row) for row in rows] if self._stale else rows

    def iter_memories(self, batch_size: int = 100) -> Iterator[Mapping]:
        self._refresh_cloud_events()
        for row in self._cache.iter_memories(batch_size):
            yield _mark_stale(row) if self._stale else row

    def set_status(self, key: str, status: str, now: int,
                   archived_at: int | None = None, event_metadata: dict | None = None) -> int:
        with self.transaction():
            return self._cache.set_status(key, status, now, archived_at, event_metadata)

    def delete_by_keys(self, keys: list[str], reason: str = "deleted") -> int:
        with self.transaction():
            return self._cache.delete_by_keys(keys, reason)

    def delete_by_canonical(self, canonical_id: str, reason: str = "purged") -> int:
        with self.transaction():
            return self._cache.delete_by_canonical(canonical_id, reason)

    def count(self) -> int:
        self._refresh_cloud_events()
        return self._cache.count()

    def count_by_status(self) -> dict[str, int]:
        self._refresh_cloud_events()
        return self._cache.count_by_status()

    def fts_search(self, text: str, limit: int = 100, status: str = "active",
                   extra: dict | None = None) -> list[Mapping]:
        self._refresh_cloud_events()
        rows = self._cache.fts_search(text, limit, status, extra)
        return [_mark_stale(row) for row in rows] if self._stale else rows

    def _vector_filter(self, status: str, filters: dict | None, now: int | None) -> dict:
        parts: list[dict] = [{"status": status}]
        for key, value in (filters or {}).items():
            if key in {"canonical_id", "task_id", "team_id", "memory_type", "agent_id"}:
                parts.append({key: value})
        if now is not None:
            parts.append({"$or": [
                {"expires_at": {"$exists": False}},
                {"expires_at": {"$gt": int(now)}},
            ]})
        return parts[0] if len(parts) == 1 else {"$and": parts}

    def knn(self, query: np.ndarray, k: int, status: str = "active",
            filters: dict | None = None, now: int | None = None) -> list[tuple[Mapping, float]]:
        if k <= 0:
            return []
        self._refresh_cloud_events()
        query = np.asarray(query, dtype=np.float32)
        if not np.any(query):
            return []
        if self._stale or not self._vector_projection_ok:
            results = self._cache.knn(query, k, status, filters, now)
            return [(_mark_stale(row), distance) for row, distance in results] if self._stale else results
        params = {
            "vectorBucketName": self._config.vector_bucket,
            "indexName": self._config.vector_index,
            "topK": min(max(k, 1), 10_000),
            "queryVector": {"float32": query.tolist()},
            "filter": self._vector_filter(status, filters, now),
            "returnDistance": True,
        }
        results: list[tuple[Mapping, float]] = []
        token = None
        while True:
            page = self._vectors.query_vectors(**params, **({"nextToken": token} if token else {}))
            for candidate in page.get("vectors", []):
                row = self.get(candidate["key"])
                if row is None or row.get("status") != status:
                    continue
                if now is not None and row.get("expires_at") is not None and int(row["expires_at"]) <= now:
                    continue
                if any(row.get(name) != value for name, value in (filters or {}).items()):
                    continue
                results.append((row, float(candidate["distance"])))
                if len(results) >= k:
                    return results
            token = page.get("nextToken")
            if not token:
                return results

    def vec_status(self) -> dict:
        return {
            "vec_in_sync": self._vector_projection_ok,
            "backend": "s3-vectors",
            "stale": self._stale,
            "projection_error": self._vector_error,
        }

    def rebuild_vec(self) -> dict:
        rebuilt = 0
        for item in self._memory_items():
            row = self._decode_row(item)
            try:
                self._project_vector(row, self._row_vector(item).tobytes())
                self._table.update_item(
                    Key=self._memory_key(str(row["key"])),
                    UpdateExpression="SET vector_projected = :yes",
                    ExpressionAttributeValues={":yes": True},
                )
            except Exception as error:
                self._vector_projection_ok = False
                self._vector_error = type(error).__name__
                return {"rebuilt": rebuilt, "vec_in_sync": False}
            rebuilt += 1
        self._vector_projection_ok = True
        self._vector_error = None
        return {"rebuilt": rebuilt, "vec_in_sync": True}

    def read_content(self, row: Mapping) -> str:
        if self._stale:
            return self._cache.read_content(row)
        content = row.get("content")
        content_hash = str(row.get("content_hash") or "")
        if isinstance(content, str):
            if f"sha256:{content_digest(content)}" != content_hash:
                raise ContentIntegrityError("stored content failed its integrity check")
            return content
        key = row.get("contentObjectKey") or self._content_key(content_hash)
        if not content_hash or not isinstance(key, str):
            raise ContentIntegrityError("stored content document is missing")
        return self._get_content_object(key, content_hash)

    def sweep_orphan_docs(self) -> int:
        referenced = {
            item.get("contentObjectKey") for item in self._memory_items()
            if item.get("contentObjectKey")
        }
        cursor = 0
        while True:
            pack = self.export_sync_events(after=cursor, limit=1000)
            if not pack["events"]:
                break
            for event in pack["events"]:
                content_key = event.get("contentObjectKey")
                if content_key:
                    referenced.add(content_key)
            cursor = int(pack["cursor"])
            if len(pack["events"]) < 1000:
                break
        removed = 0
        for prefix in (
            f"{self._identity.vault_id}/content/",
            f"{self._identity.vault_id}/events/content/",
        ):
            token = None
            while True:
                page = self._s3.list_objects_v2(
                    Bucket=self._config.content_bucket,
                    Prefix=prefix,
                    **({"ContinuationToken": token} if token else {}),
                )
                for item in page.get("Contents", []):
                    if item["Key"] not in referenced:
                        self._delete_object_versions(item["Key"])
                        removed += 1
                token = page.get("NextContinuationToken")
                if not token:
                    break
        return removed

    def _delete_object_versions(self, key: str) -> None:
        markers: dict[str, str] = {}
        while True:
            page = self._s3.list_object_versions(
                Bucket=self._config.content_bucket,
                Prefix=key,
                **markers,
            )
            versions = [
                {"Key": version["Key"], "VersionId": version["VersionId"]}
                for version in (*page.get("Versions", []), *page.get("DeleteMarkers", []))
                if version.get("Key") == key
            ]
            if versions:
                self._s3.delete_objects(
                    Bucket=self._config.content_bucket,
                    Delete={"Objects": versions, "Quiet": True},
                )
            if not page.get("IsTruncated"):
                return
            markers = {"KeyMarker": page["NextKeyMarker"]}
            if page.get("NextVersionIdMarker"):
                markers["VersionIdMarker"] = page["NextVersionIdMarker"]

    def doc_stats(self) -> dict:
        self._refresh_cloud_events()
        return self._cache.doc_stats()

    def create_server_token(self, token_id: str, token_hash: str, agent_id: str,
                            created_at: int, curator: bool = False) -> None:
        self._table.put_item(Item={
            "PK": f"VAULT#{self._identity.vault_id}#TOKEN#{token_hash}",
            "SK": "TOKEN",
            "recordType": "server-token",
            "vault_id": self._identity.vault_id,
            "token_id": token_id,
            "agent_id": agent_id,
            "created_at": created_at,
            "curator": bool(curator),
            "revoked": False,
            "GSI0PK": f"VAULT#{self._identity.vault_id}#TOKEN",
            "GSI0SK": f"TOKEN#{created_at:020d}#{token_id}",
            "GSI4PK": f"VAULT#{self._identity.vault_id}#TOKENID#{token_id}",
            "GSI4SK": "TOKEN",
        }, ConditionExpression="attribute_not_exists(PK)")

    def get_server_token(self, token_hash: str) -> Mapping | None:
        item = self._table.get_item(Key={
            "PK": f"VAULT#{self._identity.vault_id}#TOKEN#{token_hash}",
            "SK": "TOKEN",
        }, ConsistentRead=True).get("Item")
        if not item or item.get("recordType") != "server-token" or item.get("revoked"):
            return None
        return MappingProxyType({
            name: item.get(name)
            for name in ("token_id", "agent_id", "curator", "created_at")
        })

    def list_server_tokens(self) -> list[Mapping]:
        items = []
        for page in self._query_pages(
            IndexName="ByVault",
            KeyConditionExpression=self._key("GSI0PK").eq(
                f"VAULT#{self._identity.vault_id}#TOKEN"
            ),
        ):
            items.extend(item for item in page.get("Items", [])
                         if item.get("recordType") == "server-token")
        return [MappingProxyType({
            name: item.get(name)
            for name in ("token_id", "agent_id", "curator", "created_at")
        }) for item in items]

    def delete_server_token(self, token_id: str) -> int:
        matches = self._table.query(
            IndexName="ByTokenId",
            KeyConditionExpression=self._key("GSI4PK").eq(
                f"VAULT#{self._identity.vault_id}#TOKENID#{token_id}"
            ),
            Limit=1,
        )
        items = matches.get("Items", [])
        if not items:
            return 0
        item = items[0]
        response = self._table.delete_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            ReturnValues="ALL_OLD",
            ConditionExpression="#token_id = :token_id",
            ExpressionAttributeNames={"#token_id": "token_id"},
            ExpressionAttributeValues={":token_id": token_id},
        )
        return int(bool(response.get("Attributes")))

    def get_agent_ids(self, keys: list[str]) -> dict[str, str]:
        self._refresh_cloud_events()
        return self._cache.get_agent_ids(keys)

    def sync_origin_id(self) -> str:
        return self._cache.sync_origin_id()

    def _query_event_items(self, after: int, limit: int) -> list[dict]:
        items = []
        for shard in range(_EVENT_SHARDS):
            partition = f"VAULT#{self._identity.vault_id}#EVENTS#{shard:02x}"
            request = {
                "KeyConditionExpression": self._key("PK").eq(partition)
                & self._key("SK").gt(f"SEQ#{after:020d}"),
                "ConsistentRead": True,
                "Limit": min(limit + 1, 1000),
            }
            shard_items = 0
            while True:
                page = self._table.query(**request)
                found = [item for item in page.get("Items", [])
                         if item.get("recordType") == "event"]
                items.extend(found)
                shard_items += len(found)
                if shard_items >= limit + 1 or not page.get("LastEvaluatedKey"):
                    break
                request["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return sorted(items, key=lambda item: int(item.get("seq", 0)))

    def export_sync_events(self, after: int = 0, limit: int = 100_000) -> dict:
        if after < 0 or limit < 1:
            raise ValueError("sync event cursor and limit must be positive")
        candidates = self._query_event_items(after, limit + 1)
        selected = candidates[:limit]
        events = []
        for item in selected:
            event = json.loads(item["event_json"])
            content_key = item.get("contentObjectKey")
            if content_key and isinstance(event.get("snapshot"), dict):
                snapshot = event["snapshot"]
                snapshot["content"] = self._get_content_object(
                    content_key, str(snapshot.get("content_hash") or ""),
                )
            event["feed_seq"] = int(item["seq"])
            events.append(event)
        cursor = int(selected[-1]["seq"]) if selected else after
        return {"pack": "cairn-sync-2", "after": after, "cursor": cursor, "events": events}

    def has_sync_event(self, event_id: str) -> bool:
        return self._event_marker_exists(event_id)

    def _event_marker_exists(self, event_id: str) -> bool:
        response = self._table.get_item(
            Key=self._event_marker_key(event_id),
            ConsistentRead=True,
        )
        return response.get("Item", {}).get("recordType") == "event-dedupe"

    def has_sync_tombstone(self, key: str) -> bool:
        response = self._table.get_item(Key=self._tombstone_key(key), ConsistentRead=True)
        return bool(response.get("Item"))

    def apply_sync_event(self, event: dict) -> str:
        from .secret_scan import scan_content

        for content in _event_contents(event):
            scan_content(content)
        with self.transaction():
            return self._cache.apply_sync_event(event)

    def _cursor_key(self, peer: str, direction: str, token_id: str) -> dict[str, str]:
        suffix = _sha("\0".join((token_id, direction, peer)))
        return {"PK": f"VAULT#{self._identity.vault_id}#CURSORS", "SK": f"CURSOR#{suffix}"}

    def get_sync_cursor(self, peer: str, direction: str, token_id: str = "") -> int:
        response = self._table.get_item(
            Key=self._cursor_key(peer, direction, token_id), ConsistentRead=True,
        )
        return int(response.get("Item", {}).get("cursor", 0))

    def list_sync_cursors(self) -> list[dict]:
        items = []
        for page in self._query_pages(
            KeyConditionExpression=self._key("PK").eq(f"VAULT#{self._identity.vault_id}#CURSORS"),
        ):
            items.extend(page.get("Items", []))
        return [{
            "peer": item.get("peer", ""),
            "direction": item.get("direction", ""),
            "token_id": item.get("token_id", ""),
            "cursor": int(item.get("cursor", 0)),
            "updated_at": int(item.get("updated_at", 0)),
        } for item in items if item.get("recordType") == "sync-cursor"]

    def set_sync_cursor(self, peer: str, direction: str, cursor: int, now: int,
                        token_id: str = "") -> None:
        if cursor < 0 or direction not in {"push", "pull"}:
            raise ValueError("sync cursor or direction is invalid")
        value = {
            "peer": peer,
            "direction": direction,
            "token_id": token_id,
            "cursor": int(cursor),
            "updated_at": int(now),
        }
        if self._tx_depth == 0:
            with self.transaction():
                self._cache.set_sync_cursor(peer, direction, cursor, now, token_id)
                self._pending_cursors.append(value)
        else:
            self._cache.set_sync_cursor(peer, direction, cursor, now, token_id)
            self._pending_cursors.append(value)

    def list_competing_corrections(self) -> list[dict]:
        self._refresh_cloud_events()
        return self._cache.list_competing_corrections()

    def record_competing_corrections(self, now: int) -> list[dict]:
        with self.transaction():
            return self._cache.record_competing_corrections(now)

    def list_sync_conflicts(self, include_resolved: bool = False) -> list[dict]:
        self._refresh_cloud_events()
        local = self._cache.list_sync_conflicts(include_resolved)
        if local:
            return local
        items = []
        for page in self._query_pages(
            KeyConditionExpression=self._key("PK").eq(f"VAULT#{self._identity.vault_id}#CONFLICTS"),
        ):
            items.extend(page.get("Items", []))
        return [{key: value for key, value in item.items()
                 if key not in {"PK", "SK", "recordType", "vault_id"}}
                for item in items if item.get("recordType") == "sync-conflict"]

    def resolve_sync_conflict(self, base_key: str, winner_key: str,
                              resolved_by: str, now: int) -> None:
        with self.transaction():
            self._cache.resolve_sync_conflict(base_key, winner_key, resolved_by, now)

    def reopen(self) -> AwsVault:
        return AwsVault(
            self._vault_dir, self._cache._embed_name, self._cache._dims, self._config,
            create=False, doc_threshold=self.doc_threshold,
        )

    def close(self) -> None:
        self._cache.close()


def _event_contents(event: dict) -> Iterator[str]:
    content = event.get("content")
    if isinstance(content, str):
        yield content
    snapshot = event.get("snapshot")
    if isinstance(snapshot, dict) and isinstance(snapshot.get("content"), str):
        yield snapshot["content"]


def _mark_stale(row: Mapping) -> Mapping:
    return MappingProxyType({**dict(row), "storage_stale": True})
