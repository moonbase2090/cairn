"""API Gateway HTTP API implementation for Cairn's authenticated sync routes."""
from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from cairn.aws_storage import AwsVault
from cairn.client import CairnClient
from cairn.models import Status, now_epoch
from cairn.storage import FILTER_COLUMNS, StorageConfig
from cairn.sync_auth import sync_pack_matches_agent


MAX_REQUEST_BYTES = 5 * 1024 * 1024
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_PULL_EVENTS = 100
MAX_SEARCH_RESULTS = 20


class RequestError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class _MetadataEmbedder:
    """Carries the vault's embedding-space identity; sync imports need no model."""

    def __init__(self, name: str, dims: int):
        self.name = name
        self.dims = dims

    def embed(self, _texts):
        raise RuntimeError("the Cairn sync endpoint does not create embeddings")


def _new_client() -> CairnClient:
    dimensions = int(os.environ["EMBED_DIMS"])
    embedder = _MetadataEmbedder(os.environ["EMBED_MODEL"], dimensions)
    config = StorageConfig(
        backend="aws",
        region=os.environ["AWS_REGION"],
        vault_id=os.environ["VAULT_ID"],
        vault_name=os.environ["VAULT_NAME"],
        table=os.environ["MEMORY_TABLE"],
        cache_table=os.environ["CACHE_TABLE"],
        content_bucket=os.environ["CONTENT_BUCKET"],
        vector_bucket=os.environ["VECTOR_BUCKET"],
        vector_index=os.environ["VECTOR_INDEX"],
        vector_index_arn=os.environ["VECTOR_INDEX_ARN"],
    )
    vault = AwsVault(Path("/tmp/cairn-api"), embedder.name, dimensions, config, create=True)
    return CairnClient(vault, "cairn-api", embedder)


def _response(status_code: int, value: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(value, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) > MAX_RESPONSE_BYTES:
        status_code = 413
        encoded = json.dumps({"error": "response exceeds the sync API size limit"})
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json; charset=utf-8"},
        "isBase64Encoded": False,
        "body": encoded,
    }


def _body(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body") or "{}"
    if not isinstance(body, str):
        raise RequestError(400, "request body must be JSON")
    try:
        raw = base64.b64decode(body, validate=True) if event.get("isBase64Encoded") else body.encode("utf-8")
    except (ValueError, UnicodeError):
        raise RequestError(400, "request body is not valid base64") from None
    if len(raw) > MAX_REQUEST_BYTES:
        raise RequestError(413, "request body exceeds the sync API size limit")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RequestError(400, "request body is not valid JSON") from None
    if not isinstance(value, dict):
        raise RequestError(400, "request body must be a JSON object")
    return value


def _query(event: dict[str, Any]) -> dict[str, str]:
    params = event.get("queryStringParameters")
    if isinstance(params, dict):
        return {str(key): str(value) for key, value in params.items() if value is not None}
    raw = event.get("rawQueryString")
    if isinstance(raw, str):
        return {key: values[-1] for key, values in parse_qs(raw).items() if values}
    return {}


def _peer_header(event: dict[str, Any]) -> str:
    headers = event.get("headers")
    if not isinstance(headers, dict):
        raise RequestError(400, "X-Cairn-Peer header is required")
    peer = next(
        (value for name, value in headers.items()
         if isinstance(name, str) and name.lower() == "x-cairn-peer"),
        None,
    )
    if not isinstance(peer, str) or not peer or len(peer) > 128:
        raise RequestError(400, "X-Cairn-Peer header is invalid")
    return peer


def _caller(event: dict[str, Any], expected_vault_id: str) -> dict[str, Any]:
    try:
        context = event["requestContext"]["authorizer"]["lambda"]
    except (KeyError, TypeError):
        raise RequestError(403, "authorized membership context is required") from None
    if (not isinstance(context, dict)
            or context.get("vault_id") != expected_vault_id
            or not isinstance(context.get("token_id"), str) or not context["token_id"]
            or not isinstance(context.get("agent_id"), str) or not context["agent_id"]
            or not isinstance(context.get("curator"), bool)):
        raise RequestError(403, "authorized membership context is invalid")
    return context


def _health(client: CairnClient, caller: dict[str, Any], endpoint_origin_id: str) -> dict[str, Any]:
    vault = client.vault
    identity = vault.vault_identity
    return {
        "ok": True, "memories": vault.count(), "vault_id": identity.vault_id,
        "vault_name": identity.name, "origin_id": endpoint_origin_id,
        "token_id": caller["token_id"],
    }


def _push(event: dict[str, Any], client: CairnClient, caller: dict[str, Any]) -> dict[str, Any]:
    vault = client.vault
    pack = _body(event)
    peer = pack.get("origin_id")
    if not isinstance(peer, str) or not peer or len(peer) > 128:
        raise RequestError(400, "sync pack has an invalid origin identity")
    if not sync_pack_matches_agent(pack, caller["agent_id"], caller["curator"], vault):
        raise RequestError(403, "token is restricted to its assigned agent")
    after = pack.get("after")
    if isinstance(after, int) and not isinstance(after, bool):
        if after > vault.get_sync_cursor(peer, "push", caller["token_id"]):
            raise RequestError(409, "pushed cursor skips events")
    result = client.import_sync_pack(
        pack, peer=peer, direction="push", token_id=caller["token_id"],
    )
    return {"ok": True, **result}


def _pull(
    event: dict[str, Any], client: CairnClient, caller: dict[str, Any],
    endpoint_origin_id: str, exported_at: int | None,
) -> dict[str, Any]:
    vault = client.vault
    query = _query(event)
    try:
        after = int(query.get("after", "0"))
        limit = int(query.get("limit", str(MAX_PULL_EVENTS)))
    except ValueError:
        raise RequestError(400, "after and limit must be integers") from None
    if after < 0:
        raise RequestError(400, "cursor cannot be negative")
    if limit < 1 or limit > MAX_PULL_EVENTS:
        raise RequestError(400, f"limit must be from 1 through {MAX_PULL_EVENTS}")
    peer = _peer_header(event)
    saved = vault.get_sync_cursor(peer, "pull", caller["token_id"])
    if after > saved:
        raise RequestError(409, "requested cursor is ahead of the saved peer cursor")
    pack = vault.export_sync_events(after, limit)
    pack.update({
        "embed_model": client.embedder.name,
        "dims": client.embedder.dims,
        "exported_at": exported_at if exported_at is not None else now_epoch(),
        "origin_id": endpoint_origin_id,
        "vault_id": vault.vault_identity.vault_id,
    })
    while pack["events"] and len(json.dumps(pack, default=str).encode("utf-8")) > MAX_RESPONSE_BYTES:
        if len(pack["events"]) == 1:
            raise RequestError(413, "one sync event exceeds the sync API size limit")
        pack["events"].pop()
        pack["cursor"] = pack["events"][-1]["feed_seq"] if pack["events"] else after
    if len(json.dumps(pack, default=str).encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise RequestError(413, "one sync event exceeds the sync API size limit")
    with vault.transaction():
        vault.set_sync_cursor(
            peer, "pull", int(pack["cursor"]), pack["exported_at"], caller["token_id"],
        )
    return pack


def _search(event: dict[str, Any], client: CairnClient) -> dict[str, Any]:
    import numpy as np

    request = _body(event)
    vector = request.get("vector")
    if not isinstance(vector, list) or len(vector) != client.embedder.dims:
        raise RequestError(400, "vector must contain one finite number per vault dimension")
    vector_values = []
    for value in vector:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise RequestError(400, "vector must contain one finite number per vault dimension")
        try:
            converted = float(value)
        except OverflowError:
            raise RequestError(400, "vector must contain one finite number per vault dimension") from None
        if not math.isfinite(converted):
            raise RequestError(400, "vector must contain one finite number per vault dimension")
        vector_values.append(converted)
    limit = request.get("limit", 5)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_SEARCH_RESULTS:
        raise RequestError(400, f"limit must be an integer from 1 through {MAX_SEARCH_RESULTS}")
    status = request.get("status", Status.ACTIVE.value)
    if not isinstance(status, str) or status not in {value.value for value in Status}:
        raise RequestError(400, "status is invalid")
    filters = request.get("filters", {})
    if (not isinstance(filters, dict)
            or set(filters) - set(FILTER_COLUMNS)
            or any(not isinstance(value, str) for value in filters.values())):
        raise RequestError(400, "filters must contain supported string fields")
    hits = client.vault.knn(
        np.asarray(vector_values, dtype=np.float32), limit, status, filters or None,
    )
    records = [client._record(row, 1.0 - distance).to_dict() for row, distance in hits]
    identity = client.vault.vault_identity
    return {"results": records, "vault_id": identity.vault_id, "vault_name": identity.name}


def _dispatch(
    event: dict[str, Any], client: CairnClient, caller: dict[str, Any],
    endpoint_origin_id: str, exported_at: int | None = None,
) -> dict[str, Any]:
    request_context = event.get("requestContext", {})
    http = request_context.get("http", {}) if isinstance(request_context, dict) else {}
    method = http.get("method") if isinstance(http, dict) else None
    path = event.get("rawPath") or event.get("path")
    if not isinstance(path, str):
        raise RequestError(404, "route not found")
    if path == "/health" and method == "GET":
        return _health(client, caller, endpoint_origin_id)
    if path == "/push" and method == "POST":
        return _push(event, client, caller)
    if path == "/pull" and method == "GET":
        return _pull(event, client, caller, endpoint_origin_id, exported_at)
    if path == "/search" and method == "POST":
        return _search(event, client)
    raise RequestError(404, "route not found")


def handle_request(event: dict[str, Any], client: CairnClient, *,
                   endpoint_origin_id: str, embed_dims: int,
                   exported_at: int | None = None) -> dict[str, Any]:
    """Run one validated HTTP API v2 request with an injected AWS-backed client."""
    try:
        caller = _caller(event, client.vault.vault_identity.vault_id)
        if client.embedder.dims != embed_dims:
            raise RequestError(500, "sync endpoint embedding metadata is inconsistent")
        result = _dispatch(event, client, caller, endpoint_origin_id, exported_at)
        return _response(200, result)
    except RequestError as error:
        return _response(error.status_code, {"error": str(error)})
    except ValueError as error:
        return _response(400, {"error": str(error)})


_CLIENT: CairnClient | None = None


def handler(event: dict[str, Any], _context) -> dict[str, Any]:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _new_client()
    return handle_request(
        event, _CLIENT,
        endpoint_origin_id=os.environ["SYNC_ORIGIN_ID"],
        embed_dims=int(os.environ["EMBED_DIMS"]),
    )
