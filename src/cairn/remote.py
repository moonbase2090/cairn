"""Authenticated client access to a Cairn vault hosted by ``cairn serve``."""

from __future__ import annotations

import json
import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request
from urllib.parse import urlparse

from .client import CairnClient
from .models import MemoryRecord, StoreAction, StoreResult
from .storage import MemoryQuery
from .vault_identity import VaultIdentity


@dataclass(frozen=True)
class RemoteConfig:
    url: str
    token: str
    tls_ca: str | None = None


def _remote_table(path: Path) -> dict:
    try:
        import tomllib

        with path.open("rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read remote settings from {path.name}") from exc
    table = config.get("remote", {})
    if not isinstance(table, dict):
        raise ValueError(f"{path.name}: [remote] must be a table")  # noqa: TRY004 - CLI config error
    unknown = set(table) - {"url", "token", "tls_ca"}
    if unknown:
        raise ValueError(
            f"{path.name}: unknown [remote] setting(s): {', '.join(sorted(unknown))}"
        )
    if any(not isinstance(value, str) for value in table.values()):
        raise ValueError(f"{path.name}: [remote] values must be strings")
    return table


def remote_config(vault_dir: Path) -> RemoteConfig | None:
    """Resolve opt-in remote settings; a URL and token must be configured together."""
    env_url = os.environ.get("CAIRN_REMOTE_URL")
    env_token = os.environ.get("CAIRN_REMOTE_TOKEN")
    if env_url or env_token:
        if not (env_url and env_token):
            return None
        values = {
            "url": env_url,
            "token": env_token,
            "tls_ca": os.environ.get("CAIRN_REMOTE_TLS_CA"),
        }
    else:
        values = {}
        for path in (vault_dir / "config.toml", Path.home() / ".cairn" / "config.toml"):
            table = _remote_table(path)
            if table:
                values = table
                break
        if not values:
            return None
        if not values.get("url") or not values.get("token"):
            raise ValueError("[remote] needs both url and token")

    url, token = values["url"].strip(), values["token"].strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError(
            "remote url must be an HTTP(S) URL without embedded credentials"
        )
    if parsed.query or parsed.fragment:
        raise ValueError("remote url must not contain a query or fragment")
    if not token:
        raise ValueError("remote token must not be empty")
    tls_ca = values.get("tls_ca") or None
    return RemoteConfig(url.rstrip("/"), token, tls_ca)


def _context(config: RemoteConfig):
    parsed = urlparse(config.url)
    if parsed.scheme == "https":
        return ssl.create_default_context(cafile=config.tls_ca)
    host = parsed.hostname or ""
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("refusing plain HTTP to a non-local host; use https://")
    return None


class _RejectRedirects(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_request(req, config: RemoteConfig):
    handlers = [_RejectRedirects()]
    context = _context(config)
    if context is not None:
        handlers.append(request.HTTPSHandler(context=context))
    return request.build_opener(*handlers).open(req, timeout=60)


class _RemoteVault:
    name = "remote"

    def __init__(self, identity: VaultIdentity, by_hash):
        self.vault_identity = identity
        self.by_hash = by_hash


class RemoteCairnClient:
    """CairnClient's memory operations, served by the authenticated remote vault."""

    remote = True

    def __init__(
        self,
        config: RemoteConfig,
        vault_dir: Path,
        expected_agent_id: str | None = None,
    ):
        self.config = config
        self.vault_dir = vault_dir
        identity = self._request("whoami", {})
        self.agent_id = identity["agent_id"]
        if expected_agent_id and expected_agent_id != self.agent_id:
            raise ValueError("remote token belongs to a different CAIRN_AGENT")
        self.curator = bool(identity["curator"])
        self.embedder = _EmbedderInfo(identity["embedder"], int(identity["dims"]))
        self.vault = _RemoteVault(
            VaultIdentity(identity["vault_id"], identity["vault_name"]),
            self.by_hash,
        )

    def _request(self, operation: str, params: dict):
        data = json.dumps({"operation": operation, "params": params}).encode("utf-8")
        req = request.Request(
            self.config.url + "/rpc",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.token}",
            },
        )
        try:
            with _open_request(req, self.config) as response:
                return json.load(response)
        except error.HTTPError as exc:
            try:
                detail = json.load(exc).get("error", "remote request failed")
            except (ValueError, OSError):
                detail = "remote request failed"
            raise ValueError(str(detail)) from None
        except error.URLError as exc:
            raise ValueError(f"remote vault is unavailable: {exc.reason}") from None

    def whoami(self):
        return self._request("whoami", {})

    @staticmethod
    def _record(value: dict | None) -> MemoryRecord | None:
        return MemoryRecord(**value) if value is not None else None

    def store_memory(
        self,
        content: str,
        team_id: str,
        task_id: str,
        memory_type: str = "semantic",
        origin: str = "agent",
        supersedes_key: str | None = None,
        mode: str = "auto",
        provenance: str | None = None,
        vector=None,
    ) -> StoreResult:
        result = self._request(
            "store",
            {
                "content": content,
                "team_id": team_id,
                "task_id": task_id,
                "memory_type": memory_type,
                "origin": origin,
                "supersedes_key": supersedes_key,
                "mode": mode,
                "provenance": provenance,
            },
        )
        result["action"] = StoreAction(result["action"])
        result["near_duplicates"] = [
            MemoryRecord(**record) for record in result.get("near_duplicates", [])
        ]
        return StoreResult(**result)

    def retrieve_memory(
        self,
        query: str,
        filters: dict | None = None,
        top_k: int = 5,
        min_similarity: float | None = None,
    ):
        rows = self._request(
            "retrieve",
            {
                "query": query,
                "filters": filters or {},
                "top_k": top_k,
                "min_similarity": min_similarity,
            },
        )
        return [MemoryRecord(**row) for row in rows]

    def list_memories(self, filters: dict, limit: int = 100):
        rows = self._request("list", {"filters": filters, "limit": limit})
        return [MemoryRecord(**row) for row in rows]

    def get_memory(self, key: str):
        return self._record(self._request("get", {"key": key}).get("record"))

    def archive_memory(self, key: str):
        return self._request("archive", {"key": key})

    def restore_memory(self, key: str):
        return self._request("restore", {"key": key})

    def purge_memory(self, canonical_id: str):
        return self._request("purge", {"canonical_id": canonical_id})

    def by_hash(self, digest: str, task_id: str):
        return self._request("by_hash", {"digest": digest, "task_id": task_id})

    def _embed_many(self, texts: list[str]):
        # Ingestion passes its vectors to store_memory; remote store embeds there.
        return [None] * len(texts)

    def preflight_secret_scan(self):
        return self._request("secret_scan", {})


@dataclass(frozen=True)
class _EmbedderInfo:
    name: str
    dims: int


def dispatch_remote(client, operation: str, params: dict, agent_id: str, curator: bool):
    """Run one authenticated remote memory operation against the server client."""
    if operation == "whoami":
        identity = client.vault.vault_identity
        return {
            "agent_id": agent_id,
            "curator": curator,
            "vault_id": identity.vault_id,
            "vault_name": identity.name,
            "memories": client.vault.count(),
            "embedder": client.embedder.name,
            "dims": client.embedder.dims,
        }
    if operation == "by_hash":
        return _remote_by_hash(client, params)
    remote_client = CairnClient(
        client.vault, agent_id, client.embedder, audit_path=client.audit_path
    )
    handlers = {
        "store": _remote_store,
        "retrieve": _remote_retrieve,
        "list": _remote_list,
        "get": _remote_get,
        "archive": _remote_archive,
        "restore": _remote_restore,
        "purge": _remote_purge,
        "secret_scan": _remote_secret_scan,
    }
    handler = handlers.get(operation)
    if handler is None:
        raise ValueError("unsupported remote operation")
    return handler(client, remote_client, params, agent_id, curator)


def _remote_store(client, remote_client, params, agent_id, curator):
    mode = _optional_text(params, "mode", "auto")
    if mode not in {"auto", "new"}:
        raise ValueError("mode must be auto or new")
    supersedes_key = _optional_text(params, "supersedes_key", None)
    if supersedes_key:
        target = client.vault.get(supersedes_key)
        if target is None:
            raise KeyError(f"supersedes target not found: {supersedes_key}")
        if target["agent_id"] != agent_id and not curator:
            raise PermissionError(
                "curator permission is required to supersede another agent's memory"
            )
    return remote_client.store_memory(
        _required_text(params, "content"),
        _required_text(params, "team_id"),
        _required_text(params, "task_id"),
        memory_type=_optional_text(params, "memory_type", "semantic"),
        origin=_optional_text(params, "origin", "agent"),
        supersedes_key=supersedes_key,
        mode=mode,
        provenance=_optional_text(params, "provenance", None),
    ).to_dict()


def _remote_retrieve(_client, remote_client, params, _agent_id, _curator):
    query = _required_text(params, "query")
    filters = params.get("filters", {})
    if not isinstance(filters, dict):
        raise TypeError("filters must be an object")
    top_k = params.get("top_k", 5)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 100:
        raise ValueError("top_k must be between 1 and 100")
    min_similarity = params.get("min_similarity")
    if min_similarity is not None and (
        isinstance(min_similarity, bool) or not isinstance(min_similarity, (int, float))
    ):
        raise ValueError("min_similarity must be a number")
    return [
        row.to_dict()
        for row in remote_client.retrieve_memory(
            query,
            filters,
            top_k,
            min_similarity,
        )
    ]


def _remote_list(_client, remote_client, params, _agent_id, _curator):
    filters, limit = params.get("filters"), params.get("limit", 100)
    if not isinstance(filters, dict):
        raise TypeError("filters must be an object")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    return [row.to_dict() for row in remote_client.list_memories(filters, limit)]


def _remote_get(_client, remote_client, params, _agent_id, _curator):
    record = remote_client.get_memory(_required_text(params, "key"))
    return {"record": record.to_dict() if record else None}


def _remote_archive(client, remote_client, params, agent_id, curator):
    return _remote_change_status(
        client, remote_client, params, agent_id, curator, "archive"
    )


def _remote_restore(client, remote_client, params, agent_id, curator):
    return _remote_change_status(
        client, remote_client, params, agent_id, curator, "restore"
    )


def _remote_change_status(client, remote_client, params, agent_id, curator, operation):
    key = _required_text(params, "key")
    row = client.vault.get(key)
    if row is None:
        raise KeyError(f"not found: {key}")
    if row["agent_id"] != agent_id and not curator:
        raise PermissionError("token is restricted to its assigned agent")
    if operation == "restore" and row["status"] == "superseded" and not curator:
        rivals = client.vault.find(
            MemoryQuery(
                eq={"canonical_id": row["canonical_id"], "status": "active"},
                exclude_key=key,
            ),
            100,
        )
        if any(rival["agent_id"] != agent_id for rival in rivals):
            raise PermissionError(
                "curator permission is required to restore over another agent's memory"
            )
    method = (
        remote_client.archive_memory
        if operation == "archive"
        else remote_client.restore_memory
    )
    return method(key)


def _remote_purge(client, remote_client, params, agent_id, curator):
    canonical_id = _required_text(params, "canonical_id")
    rows = client.vault.find(MemoryQuery(eq={"canonical_id": canonical_id}), 1001)
    if len(rows) > 1000:
        raise ValueError("purge group exceeds the 1000 memory limit")
    if any(row["agent_id"] != agent_id for row in rows) and not curator:
        raise PermissionError(
            "curator permission is required to purge another agent's memory"
        )
    return remote_client.purge_memory(canonical_id)


def _remote_secret_scan(_client, remote_client, _params, _agent_id, _curator):
    return remote_client.preflight_secret_scan()


def _remote_by_hash(client, params):
    digest = _required_text(params, "digest")
    task_id = _required_text(params, "task_id")
    return bool(client.vault.by_hash(digest, task_id))


def _required_text(params: dict, key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_text(params: dict, key: str, default):
    value = params.get(key, default)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value
