"""Continuous SQLite backups through Litestream's WAL replication format."""
from __future__ import annotations

import contextlib
import datetime as dt
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

MIN_LITESTREAM_VERSION = (0, 5, 16)
DEFAULT_SNAPSHOT_INTERVAL = "24h"
DEFAULT_RETENTION = "168h"
DEFAULT_L0_RETENTION = "24h"
DEFAULT_SYNC_INTERVAL = 1.0
_DURATION = re.compile(r"([1-9][0-9]*)([smh])\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\Z")
_DOC_REF = re.compile(r"sha256:([0-9a-f]{64})\Z")
_CREDENTIAL_ENV = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "LITESTREAM_ACCESS_KEY_ID", "LITESTREAM_SECRET_ACCESS_KEY",
)


@dataclass(frozen=True)
class BackupTarget:
    name: str
    kind: str
    bucket: str | None
    path: str
    endpoint: str | None
    region: str
    sync_interval: float
    snapshot_interval: str
    snapshot_seconds: int
    retention: str
    l0_retention: str


@dataclass(frozen=True)
class BackupSettings:
    targets: tuple[BackupTarget, ...] = ()


def _read_config(path: Path) -> dict | None:
    try:
        with path.open("rb") as source:
            value = tomllib.load(source)
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"cannot read {path}: {error}") from error
    return value


def _duration(value: object, field: str) -> tuple[str, int]:
    if not isinstance(value, str) or not (match := _DURATION.fullmatch(value)):
        raise ValueError(f"{field} must be a positive duration such as '30s', '5m', or '24h'")
    amount = int(match.group(1))
    seconds = amount * {"s": 1, "m": 60, "h": 3600}[match.group(2)]
    return value, seconds


def _parse_target(
    raw: object, index: int, vault_dir: Path, default_name: str, config_dir: Path,
) -> BackupTarget:
    if not isinstance(raw, dict):
        raise ValueError(f"[[backup]] entry {index} must be a table")
    allowed = {
        "name", "kind", "bucket", "endpoint", "region", "path", "sync_interval",
        "snapshot_interval", "retention", "l0_retention",
    }
    extra = set(raw) - allowed
    if extra:
        if extra & {"access_key", "secret_key", "access_key_id", "secret_access_key"}:
            raise ValueError("backup credentials belong in standard environment or profile settings, not config.toml")
        raise ValueError(f"unknown [[backup]] setting(s): {', '.join(sorted(extra))}")

    kind = raw.get("kind")
    if kind == "local":
        kind = "file"
    if kind not in {"s3", "file"}:
        raise ValueError(f"[[backup]] entry {index}: kind must be 's3' or 'file'")

    requested_name = raw.get("name")
    name = requested_name if requested_name is not None else default_name
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("backup name must start with a letter or number and contain only letters, numbers, '.', '_' or '-'")

    sync_interval = raw.get("sync_interval", DEFAULT_SYNC_INTERVAL)
    if isinstance(sync_interval, bool) or not isinstance(sync_interval, (int, float)) or sync_interval <= 0:
        raise ValueError(f"backup {name!r}: sync_interval must be a positive number of seconds")
    snapshot_interval, snapshot_seconds = _duration(
        raw.get("snapshot_interval", DEFAULT_SNAPSHOT_INTERVAL),
        f"backup {name!r} snapshot_interval",
    )
    retention, _ = _duration(raw.get("retention", DEFAULT_RETENTION), f"backup {name!r} retention")
    l0_retention, _ = _duration(
        raw.get("l0_retention", DEFAULT_L0_RETENTION), f"backup {name!r} l0_retention"
    )

    if kind == "s3":
        bucket = raw.get("bucket")
        if not isinstance(bucket, str) or not bucket.strip() or "/" in bucket:
            raise ValueError(f"backup {name!r}: bucket must be a non-empty bucket name")
        endpoint = raw.get("endpoint")
        if endpoint is not None:
            parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
            if (parsed is None or parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment):
                raise ValueError(f"backup {name!r}: endpoint must be an http(s) URL without credentials, query, or fragment")
        region = raw.get("region", "us-east-1")
        if not isinstance(region, str) or not region.strip():
            raise ValueError(f"backup {name!r}: region must be a non-empty string")
        prefix = raw.get("path")
        if prefix is None:
            digest = hashlib.sha256(str((vault_dir / "vault.db").resolve()).encode()).hexdigest()[:20]
            prefix = f"cairn/{digest}"
        if not isinstance(prefix, str) or not prefix.strip() or prefix.startswith("/") or ".." in Path(prefix).parts:
            raise ValueError(f"backup {name!r}: path must be a non-empty object prefix without '..'")
        return BackupTarget(name, kind, bucket, prefix.strip("/"), endpoint, region,
                            float(sync_interval), snapshot_interval, snapshot_seconds,
                            retention, l0_retention)

    local_path = raw.get("path")
    if not isinstance(local_path, str) or not local_path.strip():
        raise ValueError(f"backup {name!r}: a local target needs path")
    folder = Path(local_path).expanduser()
    if not folder.is_absolute():
        folder = config_dir / folder
    folder = folder.resolve()
    vault = vault_dir.resolve()
    if folder == vault or folder.is_relative_to(vault):
        raise ValueError(f"backup {name!r}: local backup path must be outside the vault directory")
    return BackupTarget(name, kind, None, str(folder), None, "", float(sync_interval),
                        snapshot_interval, snapshot_seconds, retention, l0_retention)


def backup_settings(vault_dir: Path, home: Path | None = None) -> BackupSettings:
    """Read [[backup]] targets from the vault config, then the user's config."""
    if home is None:
        home = Path.home()
        xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "cairn" / "config.toml"
    else:
        xdg_config = home / ".config" / "cairn" / "config.toml"
    for path in (vault_dir / "config.toml", home / ".cairn" / "config.toml", xdg_config):
        config = _read_config(path)
        if config is None or "backup" not in config:
            continue
        raw_targets = config["backup"]
        if not isinstance(raw_targets, list):
            raise ValueError(f"{path}: use one or more [[backup]] tables")
        targets_list = []
        kind_counts: dict[str, int] = {}
        for index, raw in enumerate(raw_targets, 1):
            kind = raw.get("kind") if isinstance(raw, dict) else None
            normalized_kind = "file" if kind == "local" else kind
            kind_counts[normalized_kind] = kind_counts.get(normalized_kind, 0) + 1
            ordinal = kind_counts[normalized_kind]
            default_name = ("local" if normalized_kind == "file" else "s3")
            if ordinal > 1:
                default_name += f"-{ordinal}"
            targets_list.append(_parse_target(raw, index, vault_dir, default_name, path.parent))
        targets = tuple(targets_list)
        names = [target.name for target in targets]
        if len(set(names)) != len(names):
            raise ValueError(f"{path}: backup target names must be unique")
        return BackupSettings(targets)
    return BackupSettings()


def _quote(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _config_text(target: BackupTarget, db_path: Path, metadata_dir: Path) -> str:
    db_path = db_path.resolve()
    metadata_dir = metadata_dir.resolve()
    if target.kind == "s3":
        replica = [
            "      type: s3",
            f"      bucket: {_quote(target.bucket or '')}",
            f"      path: {_quote(target.path)}",
            f"      region: {_quote(target.region)}",
        ]
        if target.endpoint:
            replica.append(f"      endpoint: {_quote(target.endpoint)}")
        if target.endpoint:
            replica.append("      force-path-style: true")
    else:
        replica = ["      type: file", f"      path: {_quote(target.path)}"]
    lines = [
        "dbs:",
        f"  - path: {_quote(db_path)}",
        f"    meta-path: {_quote(metadata_dir / 'meta')}",
        "    replica:",
        *replica,
        "snapshot:",
        f"  interval: {target.snapshot_interval}",
        f"  retention: {target.retention}",
        f"l0-retention: {target.l0_retention}",
        "logging:",
        "  level: error",
        "  stderr: true",
    ]
    return "\n".join(lines) + "\n"


def _write_config(target: BackupTarget, db_path: Path, vault_dir: Path) -> Path:
    runtime_dir = vault_dir / "backup" / target.name
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity = json.dumps([
        str(db_path.resolve()), target.kind, target.bucket, target.path,
        target.endpoint, target.region,
    ], sort_keys=True)
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()[:20]
    metadata_dir = runtime_dir / "meta" / fingerprint
    metadata_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_path = runtime_dir / f"litestream-{fingerprint}.yml"
    content = _config_text(target, db_path, metadata_dir)
    if not config_path.exists() or config_path.read_text() != content:
        temporary = config_path.with_name(f"{config_path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(content)
        temporary.chmod(0o600)
        os.replace(temporary, config_path)
    return config_path


def _litestream() -> str:
    binary = shutil.which("litestream")
    if binary is None:
        raise FileNotFoundError(
            "backup requires Litestream 0.5.16 or later; install it from https://litestream.io/install/"
        )
    result = subprocess.run([binary, "version"], text=True, capture_output=True, check=False)
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout + result.stderr)
    if result.returncode or match is None or tuple(map(int, match.groups())) < MIN_LITESTREAM_VERSION:
        raise OSError("backup requires Litestream 0.5.16 or later")
    return binary


def _clean_error(text: str, returncode: int) -> str:
    detail = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "no details provided")
    for key in _CREDENTIAL_ENV:
        secret = os.environ.get(key)
        if secret:
            detail = detail.replace(secret, "[redacted]")
    return f"Litestream exited with status {returncode}: {detail[:500]}"


def _run_litestream(binary: str, args: list[str], stop=None) -> tuple[int, str]:
    process = subprocess.Popen(
        [binary, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    while True:
        try:
            _stdout, stderr = process.communicate(timeout=0.2 if stop is not None else None)
            return process.returncode, stderr
        except subprocess.TimeoutExpired:
            if stop is not None and stop.is_set():
                process.terminate()
                try:
                    _stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _stdout, stderr = process.communicate()
                return process.returncode, stderr


def _read_state(vault_dir: Path) -> dict:
    try:
        value = json.loads((vault_dir / "backup" / "status.json").read_text())
    except (OSError, ValueError):
        return {"targets": {}}
    return value if isinstance(value, dict) and isinstance(value.get("targets"), dict) else {"targets": {}}


def _write_state(vault_dir: Path, state: dict) -> None:
    path = vault_dir / "backup" / "status.json"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, path)


@contextlib.contextmanager
def _replication_lock(vault_dir: Path, blocking: bool):
    lock_path = vault_dir / "backup" / "replicate.lock"
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == "":
                handle.write("0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            except OSError:
                yield None
                return
        else:
            import fcntl

            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(handle.fileno(), flags)
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                yield None
                return
        yield handle
    finally:
        if not handle.closed:
            if os.name == "nt":
                import msvcrt

                with contextlib.suppress(OSError):
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _running(vault_dir: Path) -> bool:
    if not (vault_dir / "backup" / "replicate.lock").exists():
        return False
    with _replication_lock(vault_dir, blocking=False) as lock:
        return lock is None


def backup_status(vault_dir: Path, settings: BackupSettings) -> dict:
    state = _read_state(vault_dir)
    saved_targets = state["targets"]
    return {
        "running": _running(vault_dir),
        "targets": [
            {
                "name": target.name,
                "kind": target.kind,
                "last_success": saved_targets.get(target.name, {}).get("last_success"),
                "last_error": saved_targets.get(target.name, {}).get("last_error"),
                "last_snapshot": saved_targets.get(target.name, {}).get("last_snapshot"),
            }
            for target in settings.targets
        ],
    }


def _timestamp(now: float | None = None) -> str:
    value = dt.datetime.fromtimestamp(time.time() if now is None else now, dt.timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _document_refs(db_path: Path) -> list[str]:
    """Return the immutable document references used by a SQLite backup point."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(memories)")}
        if "content_ref" not in columns:
            return []
        refs = [row[0] for row in connection.execute(
            "SELECT DISTINCT content_ref FROM memories WHERE content_ref IS NOT NULL"
        )]
    for ref in refs:
        if not isinstance(ref, str) or not _DOC_REF.fullmatch(ref):
            raise OSError("vault contains an unsupported external document reference")
    return refs


def _document_bytes(vault_dir: Path, ref: str) -> bytes:
    from .models import content_digest

    match = _DOC_REF.fullmatch(ref)
    assert match is not None
    digest = match.group(1)
    source = vault_dir / "docs" / digest[:2] / digest[2:4] / f"{digest}.md"
    try:
        payload = source.read_bytes()
        text = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise OSError(f"cannot read external vault document {source}") from error
    if f"sha256:{content_digest(text)}" != ref:
        raise OSError(f"external vault document failed its content hash check: {source}")
    return payload


def _s3_client(target: BackupTarget):
    try:
        import boto3
    except ImportError as error:
        raise FileNotFoundError(
            "S3 document backups require the boto3 dependency; install Cairn with `pip install 'cairn[backup]'`"
        ) from error
    return boto3.client("s3", endpoint_url=target.endpoint, region_name=target.region)


def _s3_doc_key(target: BackupTarget, ref: str) -> str:
    digest = ref.split(":", 1)[1]
    return f"{target.path}/cairn-docs/{digest[:2]}/{digest[2:4]}/{digest}.md"


def _sync_documents(target: BackupTarget, db_path: Path, vault_dir: Path) -> None:
    """Copy referenced content-addressed files before replicating their DB rows."""
    refs = _document_refs(db_path)
    if target.kind == "file":
        remote_root = Path(target.path) / "cairn-docs"
        for ref in refs:
            digest = ref.split(":", 1)[1]
            destination = remote_root / digest[:2] / digest[2:4] / f"{digest}.md"
            payload = _document_bytes(vault_dir, ref)
            if destination.exists():
                if destination.read_bytes() == payload:
                    continue
                raise OSError(f"backup document has unexpected content: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_bytes(payload)
                os.replace(temporary, destination)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()
        return

    client = _s3_client(target)
    for ref in refs:
        digest = ref.split(":", 1)[1]
        payload = _document_bytes(vault_dir, ref)
        key = _s3_doc_key(target, ref)
        try:
            try:
                current = client.head_object(Bucket=target.bucket, Key=key)
            except client.exceptions.ClientError as error:
                code = error.response.get("Error", {}).get("Code")
                if code not in {"404", "NoSuchKey", "NotFound"}:
                    raise
                current = None
            if (current is not None
                    and current.get("Metadata", {}).get("cairn-sha256") == digest
                    and current.get("ContentLength") == len(payload)):
                continue
            client.put_object(
                Bucket=target.bucket, Key=key, Body=payload,
                Metadata={"cairn-sha256": digest},
            )
        except Exception as error:
            response = getattr(error, "response", {})
            code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
            detail = f" ({code})" if code else f" ({type(error).__name__})"
            raise OSError(f"could not sync external documents to S3 backup {target.name!r}{detail}") from None


def _fetch_document(target: BackupTarget, ref: str, client=None) -> bytes:
    digest = ref.split(":", 1)[1]
    if target.kind == "file":
        source = Path(target.path) / "cairn-docs" / digest[:2] / digest[2:4] / f"{digest}.md"
        try:
            return source.read_bytes()
        except OSError as error:
            raise OSError(f"backup is missing external document {ref}") from error
    if client is None:
        client = _s3_client(target)
    try:
        response = client.get_object(Bucket=target.bucket, Key=_s3_doc_key(target, ref))
        return response["Body"].read()
    except Exception as error:
        response = getattr(error, "response", {})
        code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
        detail = f" ({code})" if code else f" ({type(error).__name__})"
        raise OSError(f"S3 backup {target.name!r} is missing external document {ref}{detail}") from None


def _install_documents(db_path: Path, target: BackupTarget, vault_dir: Path) -> None:
    """Fetch and verify all documents before installing the restored database."""
    from .models import content_digest

    refs = _document_refs(db_path)
    if not refs:
        return
    client = _s3_client(target) if target.kind == "s3" else None
    docs_root = vault_dir / "docs"
    with tempfile.TemporaryDirectory(prefix="cairn-docs-", dir=vault_dir) as staging:
        staging_root = Path(staging)
        for ref in refs:
            match = _DOC_REF.fullmatch(ref)
            assert match is not None
            digest = match.group(1)
            payload = _fetch_document(target, ref, client)
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as error:
                raise OSError(f"backup document {ref} is not valid UTF-8") from error
            if f"sha256:{content_digest(text)}" != ref:
                raise OSError(f"backup document {ref} failed its content hash check")
            staged = staging_root / digest[:2] / digest[2:4] / f"{digest}.md"
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(payload)

        for staged in staging_root.rglob("*.md"):
            relative = staged.relative_to(staging_root)
            destination = docs_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(staged, destination)
            except FileExistsError:
                if destination.read_bytes() != staged.read_bytes():
                    raise OSError(f"existing vault document has unexpected content: {destination}")


def _run_target_once(target: BackupTarget, db_path: Path, vault_dir: Path,
                     binary: str, state: dict, stop=None) -> None:
    now = time.time()
    previous = state["targets"].get(target.name, {})
    identity = json.dumps([
        str(db_path.resolve()), target.kind, target.bucket, target.path,
        target.endpoint, target.region,
    ], sort_keys=True)
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()[:20]
    last_snapshot = previous.get("last_snapshot")
    try:
        last_snapshot_time = dt.datetime.fromisoformat(last_snapshot.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        last_snapshot_time = 0
    take_snapshot = (previous.get("fingerprint") != fingerprint
                     or now - last_snapshot_time >= target.snapshot_seconds)
    _sync_documents(target, db_path, vault_dir)
    config_path = _write_config(target, db_path, vault_dir)
    args = ["replicate", "-once", "-enforce-retention"]
    if take_snapshot:
        args.append("-force-snapshot")
    args.extend(["-config", str(config_path), "-log-level", "error"])
    returncode, stderr = _run_litestream(binary, args, stop=stop)
    result = dict(previous)
    if stop is not None and stop.is_set():
        return
    if returncode == 0:
        result.update(last_success=_timestamp(), last_error=None, fingerprint=fingerprint)
        if take_snapshot:
            result["last_snapshot"] = _timestamp()
    else:
        result["last_error"] = _clean_error(stderr, returncode)
    state["targets"][target.name] = result
    _write_state(vault_dir, state)


def replicate(vault_dir: Path, settings: BackupSettings) -> int:
    if not settings.targets:
        raise ValueError("no backup targets are configured; add one or more [[backup]] tables")
    db_path = vault_dir / "vault.db"
    if not db_path.is_file():
        raise FileNotFoundError(f"no SQLite vault at {db_path} — run `cairn init` first")
    binary = _litestream()
    stop = threading.Event()
    old_handlers = {}

    def request_stop(_signum, _frame):
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        old_handlers[signum] = signal.signal(signum, request_stop)
    try:
        with _replication_lock(vault_dir, blocking=False) as lock:
            if lock is None:
                raise OSError("backup replication is already running for this vault")
            state = _read_state(vault_dir)
            print(f"replicating SQLite backups to {len(settings.targets)} target(s)", flush=True)
            while not stop.is_set():
                for target in settings.targets:
                    if stop.is_set():
                        break
                    try:
                        _run_target_once(target, db_path, vault_dir, binary, state, stop)
                    except OSError as error:
                        previous = state["targets"].get(target.name, {})
                        previous["last_error"] = str(error)
                        state["targets"][target.name] = previous
                        _write_state(vault_dir, state)
                interval = min(target.sync_interval for target in settings.targets)
                stop.wait(interval)
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
    return 0


def _target_named(settings: BackupSettings, name: str) -> BackupTarget:
    for target in settings.targets:
        if target.name == name:
            return target
    available = ", ".join(target.name for target in settings.targets) or "none configured"
    raise ValueError(f"unknown backup target {name!r}; available: {available}")


def _validate_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("--at must be an ISO 8601 timestamp with a timezone, such as 2026-09-29T18:00:00Z") from error
    if parsed.tzinfo is None:
        raise ValueError("--at must include a timezone, for example 'Z' or '+00:00'")
    normalized = parsed.astimezone(dt.timezone.utc)
    precision = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=precision).replace("+00:00", "Z")


def _restore_metadata(vault_dir: Path, db_path: Path) -> None:
    from .embed import format_embedder_hint

    with sqlite3.connect(db_path) as connection:
        values = dict(connection.execute("SELECT k, v FROM meta WHERE k IN ('embed_model', 'dims')"))
        identity = connection.execute(
            "SELECT team_id, agent_id FROM memories ORDER BY rowid LIMIT 1"
        ).fetchone()
    model, dims = values.get("embed_model"), values.get("dims")
    if model and dims and dims.isdigit():
        (vault_dir / "embedder").write_text(format_embedder_hint(model, int(dims)))
    if not (vault_dir / "project.json").exists():
        project = vault_dir.parent.name if vault_dir.name == ".cairn" else vault_dir.name
        team = identity[0] if identity else project
        agent = os.environ.get("CAIRN_AGENT") or (identity[1] if identity else "cairn-cli")
        (vault_dir / "project.json").write_text(json.dumps({
            "project": project, "slug": project.lower().replace(" ", "-"),
            "team": team, "agent_id": agent,
        }, indent=2) + "\n")
    gitignore = vault_dir / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    lines = set(existing.splitlines())
    additions = [name for name in ("vault.db-wal", "vault.db-shm") if name not in lines]
    if additions:
        with gitignore.open("a") as output:
            if existing and not existing.endswith("\n"):
                output.write("\n")
            output.write("\n".join(additions) + "\n")


def restore(vault_dir: Path, settings: BackupSettings, name: str, at: str | None = None) -> dict:
    target = _target_named(settings, name)
    timestamp = _validate_timestamp(at)
    db_path = vault_dir / "vault.db"
    sidecars = [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"), Path(f"{db_path}-journal")]
    if any(path.exists() for path in sidecars):
        raise FileExistsError(f"{db_path} or one of its SQLite sidecars already exists; move it aside before restoring")
    binary = _litestream()
    vault_dir.mkdir(parents=True, exist_ok=True)
    config_path = _write_config(target, db_path, vault_dir)
    with tempfile.TemporaryDirectory(prefix="cairn-restore-", dir=vault_dir) as temporary_dir:
        output_path = Path(temporary_dir) / "vault.db"
        args = ["restore", "-config", str(config_path), "-o", str(output_path), "-integrity-check", "full", "-json"]
        if timestamp:
            args.extend(["-timestamp", timestamp])
        args.append(str(db_path.resolve()))
        code, stderr = _run_litestream(binary, args)
        if code:
            raise OSError(_clean_error(stderr, code))
        if not output_path.is_file():
            raise OSError("Litestream reported success without creating a restored database")
        with sqlite3.connect(output_path) as connection:
            check = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise OSError(f"restored SQLite database failed its integrity check: {check}")
        _install_documents(output_path, target, vault_dir)
        os.replace(output_path, db_path)
    _restore_metadata(vault_dir, db_path)
    return {"restored": str(db_path), "target": target.name, "at": timestamp or "latest"}
