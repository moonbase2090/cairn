"""Scheduled, idempotent cleanup for tombstoned AWS vault rows."""
from __future__ import annotations

import os

import boto3
from boto3.dynamodb.conditions import Attr, Key
def handler(_event, _context):
    table_name = os.environ["MEMORY_TABLE"]
    bucket_name = os.environ["CONTENT_BUCKET"]
    vector_bucket = os.environ["VECTOR_BUCKET"]
    vector_index = os.environ["VECTOR_INDEX"]
    vault_id = os.environ["VAULT_ID"]

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(table_name)
    s3 = boto3.client("s3")
    vectors = boto3.client("s3vectors")
    tombstone_partition = f"VAULT#{vault_id}#TOMBSTONE"
    memory_prefix = f"VAULT#{vault_id}#MEMORY#"
    removed_objects = 0
    removed_vectors = 0
    processed = 0
    kwargs = {
        "IndexName": "ByVault",
        "KeyConditionExpression": Key("GSI0PK").eq(tombstone_partition),
    }

    while True:
        page = table.query(**kwargs)
        for item in page.get("Items", []):
            if item.get("contentCleanupDone"):
                continue
            content_hash = item.get("content_hash")
            object_key = item.get("contentObjectKey")
            content_done = not (content_hash and object_key)
            if content_hash and object_key:
                has_owner = False
                for shard in range(16):
                    request = {
                        "KeyConditionExpression": Key("PK").eq(f"{memory_prefix}{shard:02x}"),
                        "FilterExpression": Attr("recordType").eq("memory") & Attr("content_hash").eq(content_hash),
                        "ConsistentRead": True,
                    }
                    while True:
                        page = table.query(**request)
                        if page.get("Items"):
                            has_owner = True
                            break
                        last_key = page.get("LastEvaluatedKey")
                        if not last_key:
                            break
                        request["ExclusiveStartKey"] = last_key
                    if has_owner:
                        break
                if not has_owner:
                    _delete_versions(s3, bucket_name, object_key)
                    removed_objects += 1
                    content_done = True
            key = item.get("key")
            if isinstance(key, str) and key and not item.get("vectorCleanupDone"):
                vectors.delete_vectors(
                    vectorBucketName=vector_bucket,
                    indexName=vector_index,
                    keys=[key],
                )
                removed_vectors += 1
            values = {":done": True}
            update = "SET vectorCleanupDone = :done"
            if content_done:
                update += ", contentCleanupDone = :done"
            table.update_item(
                Key={"PK": item["PK"], "SK": item["SK"]},
                UpdateExpression=update,
                ExpressionAttributeValues=values,
            )
            processed += 1
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key

    return {
        "processed": processed,
        "removed_content_objects": removed_objects,
        "removed_vectors": removed_vectors,
        "vault_id": vault_id,
    }


def _delete_versions(s3, bucket_name: str, object_key: str) -> None:
    markers = {}
    while True:
        page = s3.list_object_versions(
            Bucket=bucket_name, Prefix=object_key, **markers,
        )
        objects = [
            {"Key": version["Key"], "VersionId": version["VersionId"]}
            for version in (*page.get("Versions", []), *page.get("DeleteMarkers", []))
            if version.get("Key") == object_key
        ]
        if objects:
            result = s3.delete_objects(
                Bucket=bucket_name, Delete={"Objects": objects, "Quiet": True},
            )
            if result.get("Errors"):
                raise RuntimeError("vault content version cleanup failed")
        if not page.get("IsTruncated"):
            return
        markers = {"KeyMarker": page["NextKeyMarker"]}
        if page.get("NextVersionIdMarker"):
            markers["VersionIdMarker"] = page["NextVersionIdMarker"]
