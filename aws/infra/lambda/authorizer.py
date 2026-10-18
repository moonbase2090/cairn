"""HTTP API authorizer for Cairn per-agent vault tokens."""
from __future__ import annotations

import hashlib
import os
from typing import Any


def _bearer_token(event: dict[str, Any]) -> str | None:
    headers = event.get("headers")
    if isinstance(headers, dict):
        authorization = next(
            (value for name, value in headers.items()
             if isinstance(name, str) and name.lower() == "authorization"),
            None,
        )
    else:
        authorization = None
    if authorization is None:
        sources = event.get("identitySource")
        if isinstance(sources, list) and sources:
            authorization = sources[0]
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ")
    if not token or token.strip() != token:
        return None
    return token


def authorize_request(event: dict[str, Any], table, vault_id: str) -> dict[str, Any]:
    """Check a token digest against the selected vault's active membership row."""
    token = _bearer_token(event)
    if token is None:
        return {"isAuthorized": False}
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    item = table.get_item(
        Key={
            "PK": f"VAULT#{vault_id}#TOKEN#{token_hash}",
            "SK": "TOKEN",
        },
        ConsistentRead=True,
    ).get("Item")
    if not isinstance(item, dict):
        return {"isAuthorized": False}
    token_id = item.get("token_id")
    agent_id = item.get("agent_id")
    curator = item.get("curator")
    if (item.get("recordType") != "server-token"
            or item.get("vault_id") != vault_id
            or item.get("revoked") is not False
            or not isinstance(token_id, str) or not token_id
            or not isinstance(agent_id, str) or not agent_id
            or not isinstance(curator, bool)):
        return {"isAuthorized": False}
    return {
        "isAuthorized": True,
        "context": {
            "vault_id": vault_id,
            "token_id": token_id,
            "agent_id": agent_id,
            "curator": curator,
        },
    }


def handler(event: dict[str, Any], _context) -> dict[str, Any]:
    import boto3

    table_name = os.environ["MEMORY_TABLE"]
    vault_id = os.environ["VAULT_ID"]
    table = boto3.resource("dynamodb").Table(table_name)
    return authorize_request(event, table, vault_id)
