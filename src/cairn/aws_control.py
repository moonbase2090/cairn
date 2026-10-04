"""Opt-in AWS setup control plane; importing Cairn never imports an AWS SDK."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from collections import Counter


AWS_ACCOUNT_CONFIRMATION = "904233124492"
AWS_CLI_INSTALL = "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
CDK_PRICING_LINKS = {
    "S3": "https://aws.amazon.com/s3/pricing/",
    "S3 Vectors": "https://aws.amazon.com/s3/pricing/",
    "DynamoDB": "https://aws.amazon.com/dynamodb/pricing/",
    "KMS": "https://aws.amazon.com/kms/pricing/",
    "Lambda": "https://aws.amazon.com/lambda/pricing/",
    "CloudWatch": "https://aws.amazon.com/cloudwatch/pricing/",
    "EventBridge": "https://aws.amazon.com/eventbridge/pricing/",
    "API Gateway": "https://aws.amazon.com/api-gateway/pricing/",
}
CDK_PRICING_UNITS = {
    "S3": "current and previous object versions (GB-month), requests, and data transfer",
    "S3 Vectors": "logical vector storage, vector writes, queries, and GB processed",
    "DynamoDB": "on-demand reads/writes, stored data, and point-in-time recovery backups",
    "KMS": "customer-managed key-months and API requests",
    "Lambda": "requests and compute duration; varies with runtime and memory",
    "CloudWatch": "alarms, log ingestion, and log storage",
    "EventBridge": "scheduled event delivery and related requests",
    "API Gateway": "HTTP API requests; varies with region, traffic, and data transfer",
}


@dataclass(frozen=True)
class AwsPlan:
    plan_id: str
    account: str
    region: str
    profile: str | None
    vault_id: str
    vault_name: str
    dimensions: int
    stack_name: str
    infra_hash: str
    created_at: str
    source_fingerprint: str = ""
    source_memory_count: int = 0
    source_event_count: int = 0
    embed_model: str = "hash"
    enable_sync_endpoint: bool = False


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _infra_hash() -> str:
    root = _repo_root()
    infra = root / "aws" / "infra"
    files = [
        root / "pyproject.toml", root / "README.md",
        infra / "cdk.json", infra / "package.json", infra / "package-lock.json",
        infra / "lambda" / "Dockerfile",
    ]
    files.extend(sorted((infra / "bin").rglob("*.ts")))
    files.extend(sorted((infra / "lib").rglob("*.ts")))
    files.extend(sorted((infra / "lambda").rglob("*.py")))
    files.extend(sorted((root / "src" / "cairn").rglob("*.py")))
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _plan_path(vault_dir: Path, plan_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{16}", plan_id):
        raise ValueError("plan_id must be a 16-character Cairn AWS plan ID")
    return Path(vault_dir) / "aws" / "plans" / f"{plan_id}.json"


def _command_env(profile: str | None, region: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if profile:
        env["AWS_PROFILE"] = profile
    else:
        env.pop("AWS_PROFILE", None)
    if region:
        env["AWS_DEFAULT_REGION"] = region
    return env


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _aws_identity(profile: str | None, region: str) -> dict[str, str]:
    if shutil.which("aws") is None:
        raise ValueError(
            "AWS CLI is required for AWS setup; install it from " + AWS_CLI_INSTALL
        )
    command = ["aws", "sts", "get-caller-identity", "--output", "json", "--region", region]
    if profile:
        command.extend(["--profile", profile])
    result = _run(command, cwd=_repo_root(), env=_command_env(profile, region))
    if result.returncode:
        raise ValueError("AWS CLI could not verify the selected credentials or profile")
    try:
        identity = json.loads(result.stdout)
    except ValueError as error:
        raise ValueError("AWS CLI returned an invalid caller identity response") from error
    account = identity.get("Account")
    arn = identity.get("Arn")
    if not isinstance(account, str) or not re.fullmatch(r"\d{12}", account):
        raise ValueError("AWS CLI caller identity did not include a valid account ID")
    if not isinstance(arn, str) or not arn.startswith("arn:"):
        raise ValueError("AWS CLI caller identity did not include an ARN")
    return {"account": account, "arn": arn}


def check_tools() -> dict[str, str]:
    """Report tool versions without installing packages or contacting AWS services."""
    versions: dict[str, str] = {}
    for name, args in (("aws", ["--version"]), ("node", ["--version"]),
                       ("npm", ["--version"])):
        executable = shutil.which(name)
        if executable is None:
            versions[name] = "missing"
            continue
        result = _run([executable, *args], cwd=_repo_root(), env=os.environ.copy())
        output = (result.stdout or result.stderr).strip().splitlines()
        versions[name] = output[0] if result.returncode == 0 and output else "unavailable"
    infra = _repo_root() / "aws" / "infra"
    cdk = infra / "node_modules" / ".bin" / "cdk"
    if cdk.exists():
        result = _run([str(cdk), "--version"], cwd=infra, env=os.environ.copy())
        versions["cdk"] = result.stdout.strip() if result.returncode == 0 else "unavailable"
    else:
        versions["cdk"] = "needs local npm install"
    return versions


def _configured_region(profile: str | None) -> str | None:
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        if os.environ.get(name, "").strip():
            return os.environ[name].strip()
    if shutil.which("aws") is None:
        return None
    if profile is not None and not re.fullmatch(r"[A-Za-z0-9_+=,.@-]{1,64}", profile):
        raise ValueError("AWS profile name contains unsupported characters")
    command = ["aws", "configure", "get", "region"]
    if profile:
        command.extend(["--profile", profile])
    result = _run(command, cwd=_repo_root(), env=_command_env(profile))
    region = result.stdout.strip()
    return region if result.returncode == 0 and region else None


def _ensure_cdk() -> Path:
    infra = _repo_root() / "aws" / "infra"
    if shutil.which("npm") is None:
        raise ValueError("Node.js and npm are required to run the pinned local AWS CDK")
    lock_hash = hashlib.sha256((infra / "package-lock.json").read_bytes()).hexdigest()
    marker = infra / "node_modules" / ".cairn-lock-hash"
    installed_hash = marker.read_text().strip() if marker.exists() else ""
    if installed_hash != lock_hash or not (infra / "node_modules" / ".bin" / "cdk").exists():
        result = _run(["npm", "ci"], cwd=infra, env=_command_env(None))
        if result.returncode:
            raise ValueError("could not install the pinned local CDK dependencies")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(lock_hash + "\n")
    return infra


def _synthesize(plan: AwsPlan, infra: Path) -> None:
    env = _command_env(plan.profile, plan.region)
    env["CDK_DEFAULT_ACCOUNT"] = plan.account
    command = ["npm", "run", "synth", "--", *_context_args(plan)]
    result = _run(command, cwd=infra, env=env)
    if result.returncode:
        raise ValueError("local AWS CDK synthesis failed; no AWS resources were changed")


def _context_args(plan: AwsPlan) -> list[str]:
    return [
        "--context", f"vaultId={plan.vault_id}",
        "--context", f"vaultName={plan.vault_name}",
        "--context", f"dimensions={plan.dimensions}",
        "--context", f"region={plan.region}",
        "--context", f"embedModel={plan.embed_model}",
        "--context", f"enableSyncEndpoint={str(plan.enable_sync_endpoint).lower()}",
    ]


def _stack_outputs(plan: AwsPlan) -> dict[str, str]:
    command = [
        "aws", "cloudformation", "describe-stacks", "--stack-name", plan.stack_name,
        "--region", plan.region, "--output", "json",
    ]
    if plan.profile:
        command.extend(["--profile", plan.profile])
    result = _run(command, cwd=_repo_root(), env=_command_env(plan.profile, plan.region))
    if result.returncode:
        raise ValueError("AWS stack outputs could not be read after deployment")
    try:
        outputs = json.loads(result.stdout)["Stacks"][0].get("Outputs", [])
        values = {item["OutputKey"]: item["OutputValue"] for item in outputs}
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise ValueError("AWS returned an invalid stack output response") from error
    required = {
        "VaultId", "VaultName", "MemoryTableName", "EmbeddingCacheTableName",
        "ContentBucketName", "VectorBucketName", "VectorIndexName", "VectorIndexArn",
    }
    if plan.enable_sync_endpoint:
        required.add("SyncApiUrl")
    if not required.issubset(values):
        raise ValueError("AWS stack is missing outputs required to configure Cairn storage")
    if values["VaultId"] != plan.vault_id or values["VaultName"] != plan.vault_name:
        raise ValueError("AWS stack outputs do not match the reviewed vault identity")
    if plan.enable_sync_endpoint and not values["SyncApiUrl"].startswith("https://"):
        raise ValueError("AWS stack returned an invalid sync API endpoint")
    return values


def _write_storage_config(vault_dir: Path, plan: AwsPlan, outputs: dict[str, str]) -> Path:
    settings = {
        "backend": "aws",
        "region": plan.region,
        "vault_id": plan.vault_id,
        "vault_name": plan.vault_name,
        "table": outputs["MemoryTableName"],
        "cache_table": outputs["EmbeddingCacheTableName"],
        "content_bucket": outputs["ContentBucketName"],
        "vector_bucket": outputs["VectorBucketName"],
        "vector_index": outputs["VectorIndexName"],
        "vector_index_arn": outputs["VectorIndexArn"],
    }
    if plan.enable_sync_endpoint:
        settings["sync_endpoint"] = outputs["SyncApiUrl"]
    if plan.profile:
        settings["profile"] = plan.profile
    body = "[storage]\n" + "".join(
        f"{name} = {json.dumps(value)}\n" for name, value in settings.items()
    )
    path = Path(vault_dir) / "aws" / "storage.toml"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(".toml.tmp")
    temporary.write_text(body)
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def _source_events(source):
    cursor = 0
    while True:
        pack = source.export_sync_events(after=cursor, limit=1000)
        events = pack["events"]
        if not events:
            return
        yield events
        cursor = int(pack["cursor"])
        if len(events) < 1000:
            return


def _migration_preflight(source) -> dict:
    """Hash and scan SQLite contents without returning text or memory identifiers."""
    if source.name == "aws":
        return {
            "source_backend": "aws", "memory_count": source.count(),
            "event_count": 0, "content_scanned": 0, "findings": {},
            "safe": True, "fingerprint": "aws-existing",
        }
    if source.name != "sqlite":
        raise ValueError("AWS setup can migrate only a SQLite vault in this release")

    from .secret_scan import find_secret_category
    from .storage import MEMORY_FIELDS

    fingerprint = hashlib.sha256()
    fingerprint.update(str(getattr(source, "_embed_name", "")).encode("utf-8"))
    findings: Counter[str] = Counter()
    content_scanned = 0
    memory_count = 0
    for row in source.iter_memories():
        content = source.read_content(row)
        content_scanned += 1
        category = find_secret_category(content)
        if category:
            findings[category] += 1
        vector_row = source.conn.execute(
            "SELECT embedding FROM memories WHERE key=?", (row["key"],),
        ).fetchone()
        fingerprint.update(json.dumps(
            {field: row[field] for field in MEMORY_FIELDS},
            sort_keys=True, separators=(",", ":"), default=str,
        ).encode())
        if vector_row is not None:
            fingerprint.update(hashlib.sha256(bytes(vector_row["embedding"])).digest())
        memory_count += 1

    event_count = 0
    for events in _source_events(source):
        for event in events:
            for content in (
                event.get("content"),
                event.get("snapshot", {}).get("content")
                if isinstance(event.get("snapshot"), dict) else None,
            ):
                if isinstance(content, str):
                    content_scanned += 1
                    category = find_secret_category(content)
                    if category:
                        findings[category] += 1
            fingerprint.update(json.dumps(
                event, sort_keys=True, separators=(",", ":"), default=str,
            ).encode())
            event_count += 1
    cursor_rows = source.list_sync_cursors()
    for cursor in cursor_rows:
        fingerprint.update(json.dumps(
            cursor, sort_keys=True, separators=(",", ":"), default=str,
        ).encode())
    token_rows = source.conn.execute(
        "SELECT token_id, token_hash, agent_id, created_at, curator FROM server_tokens",
    ).fetchall()
    for token in token_rows:
        fingerprint.update(json.dumps(
            dict(token), sort_keys=True, separators=(",", ":"), default=str,
        ).encode())
    conflicts = source.list_sync_conflicts(include_resolved=True)
    for conflict in conflicts:
        fingerprint.update(json.dumps(
            conflict, sort_keys=True, separators=(",", ":"), default=str,
        ).encode())
    return {
        "source_backend": "sqlite", "memory_count": memory_count,
        "event_count": event_count, "content_scanned": content_scanned,
        "cursor_count": len(cursor_rows), "token_count": len(token_rows),
        "conflict_count": len(conflicts),
        "findings": dict(sorted(findings.items())), "safe": not findings,
        "fingerprint": fingerprint.hexdigest(),
    }


def _migrate_sqlite_to_aws(source, destination) -> dict:
    if source.name != "sqlite" or destination.name != "aws":
        raise ValueError("AWS migration requires a SQLite source and AWS destination")
    if source.vault_identity != destination.vault_identity:
        raise ValueError("source and AWS destination vault identities do not match")

    copied_events = 0
    for events in _source_events(source):
        for event in events:
            destination.apply_sync_event(event)
            copied_events += 1

    from .storage import MEMORY_FIELDS
    import numpy as np

    copied_memories = 0
    expected: dict[str, str] = {}
    for row in source.iter_memories():
        key = str(row["key"])
        expected[key] = str(row["content_hash"])
        if destination.get(key) is not None:
            continue
        local = source.conn.execute(
            "SELECT embedding FROM memories WHERE key=?", (key,),
        ).fetchone()
        if local is None:
            raise ValueError("SQLite migration could not read a local embedding")
        record = {field: row[field] for field in MEMORY_FIELDS}
        record["content"] = source.read_content(row)
        record["state_event_id"] = ""
        vector = np.frombuffer(bytes(local["embedding"]), dtype=np.float32).copy()
        destination.insert(record, vector)
        copied_memories += 1

    copied_cursors = 0
    for cursor in source.list_sync_cursors():
        destination.set_sync_cursor(
            cursor["peer"], cursor["direction"], int(cursor["cursor"]),
            int(cursor["updated_at"]), cursor.get("token_id", ""),
        )
        copied_cursors += 1

    token_rows = source.conn.execute(
        "SELECT token_id, token_hash, agent_id, created_at, curator FROM server_tokens",
    ).fetchall()
    copied_tokens = 0
    for token in token_rows:
        existing = destination.get_server_token(token["token_hash"])
        if existing is not None:
            if (existing.get("token_id") != token["token_id"]
                    or existing.get("agent_id") != token["agent_id"]
                    or bool(existing.get("curator")) != bool(token["curator"])):
                raise ValueError("AWS token records conflict with the SQLite vault")
            continue
        destination.create_server_token(
            token["token_id"], token["token_hash"], token["agent_id"],
            int(token["created_at"]), bool(token["curator"]),
        )
        copied_tokens += 1

    conflicts = source.list_sync_conflicts(include_resolved=True)
    for conflict in conflicts:
        base_key = str(conflict.get("base_key", ""))
        if not base_key:
            continue
        destination._table.put_item(Item={
            "PK": f"VAULT#{destination.vault_identity.vault_id}#CONFLICTS",
            "SK": f"BASE#{base_key}",
            "recordType": "sync-conflict",
            "vault_id": destination.vault_identity.vault_id,
            **conflict,
        })

    actual = {
        str(row["key"]): str(row["content_hash"])
        for row in destination._memory_items()
    }
    if actual != expected:
        raise ValueError("AWS migration verification failed; storage was not switched")
    if not destination.vec_status()["vec_in_sync"] and not destination.rebuild_vec()["vec_in_sync"]:
        raise ValueError("AWS vector migration verification failed; storage was not switched")
    return {
        "memories": len(expected), "events": copied_events,
        "cursors": copied_cursors, "tokens": copied_tokens,
        "conflicts": len(conflicts),
    }


def _plan_data(plan: AwsPlan) -> dict:
    result = asdict(plan)
    result.pop("source_fingerprint", None)
    result["confirmation_phrase"] = (
        f"DEPLOY MB2090 ACCOUNT {plan.plan_id}"
        if plan.account == AWS_ACCOUNT_CONFIRMATION
        else f"DEPLOY {plan.plan_id}"
    )
    services = CDK_PRICING_LINKS
    if not plan.enable_sync_endpoint:
        services = {name: url for name, url in CDK_PRICING_LINKS.items() if name != "API Gateway"}
    result["pricing_preview"] = [
        {"service": service, "units": CDK_PRICING_UNITS[service], "url": url}
        for service, url in services.items()
    ]
    result["estimate"] = None
    result["estimate_note"] = (
        "No monthly total is estimated because vault size, query rate, and data-transfer usage are not known."
    )
    return result


def create_plan(
    vault_dir: Path,
    vault_id: str,
    vault_name: str,
    dimensions: int,
    region: str,
    profile: str | None = None,
    source_fingerprint: str = "",
    source_memory_count: int = 0,
    source_event_count: int = 0,
    embed_model: str = "hash",
    enable_sync_endpoint: bool = False,
) -> dict:
    """Verify the signed-in identity and synthesize a plan; never creates a change set."""
    region = region.strip() or _configured_region(profile) or ""
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", region):
        raise ValueError("region must be an AWS region name such as us-west-2")
    if not re.fullmatch(r"[a-f0-9]{32}", vault_id):
        raise ValueError("vault identity must be a 32-character logical vault ID")
    if not isinstance(dimensions, int) or not 1 <= dimensions <= 4096:
        raise ValueError("embedding dimensions must be from 1 through 4096")
    if profile is not None and not re.fullmatch(r"[A-Za-z0-9_+=,.@-]{1,64}", profile):
        raise ValueError("AWS profile name contains unsupported characters")
    if not isinstance(embed_model, str) or not embed_model.strip():
        raise ValueError("embed model must be a non-empty string")
    embed_model = embed_model.strip()
    if len(embed_model) > 256 or any(ord(character) < 32 or ord(character) == 127 for character in embed_model):
        raise ValueError("embed model must be at most 256 printable characters")
    if not isinstance(enable_sync_endpoint, bool):
        raise ValueError("enable_sync_endpoint must be a boolean")

    identity = _aws_identity(profile, region)
    plan_material = "|".join((identity["account"], region, profile or "", vault_id,
                               vault_name, str(dimensions), embed_model, str(enable_sync_endpoint),
                               _infra_hash(), source_fingerprint))
    plan_id = hashlib.sha256(plan_material.encode("utf-8")).hexdigest()[:16]
    stack_name = f"cairn-vault-{vault_id}-{vault_id[:8]}"
    plan = AwsPlan(
        plan_id=plan_id,
        account=identity["account"],
        region=region,
        profile=profile,
        vault_id=vault_id,
        vault_name=vault_name,
        dimensions=dimensions,
        stack_name=stack_name,
        infra_hash=_infra_hash(),
        created_at=datetime.now(timezone.utc).isoformat(),
        source_fingerprint=source_fingerprint,
        source_memory_count=source_memory_count,
        source_event_count=source_event_count,
        embed_model=embed_model,
        enable_sync_endpoint=enable_sync_endpoint,
    )
    infra = _ensure_cdk()
    _synthesize(plan, infra)
    path = _plan_path(vault_dir, plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(plan), sort_keys=True, indent=2) + "\n")
    path.chmod(0o600)
    preview = _plan_data(plan)
    preview["caller_arn"] = identity["arn"]
    preview["plan_file"] = str(path)
    preview["synthesis"] = "complete; this is not an AWS change set and no resources were changed"
    return preview


def _load_plan(vault_dir: Path, plan_id: str) -> AwsPlan:
    path = _plan_path(vault_dir, plan_id)
    try:
        raw = json.loads(path.read_text())
        return AwsPlan(**raw)
    except FileNotFoundError as error:
        raise ValueError("AWS plan was not found; create a new plan before applying") from error
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError("saved AWS plan is invalid; create a new plan") from error


def apply_plan(
    vault_dir: Path,
    vault_id: str,
    vault_name: str,
    dimensions: int,
    plan_id: str,
    confirm: str,
    source_vault=None,
) -> dict:
    plan = _load_plan(vault_dir, plan_id)
    if plan.vault_id != vault_id or plan.vault_name != vault_name or plan.dimensions != dimensions:
        raise ValueError("AWS plan does not match the current vault identity or embedder")
    if plan.infra_hash != _infra_hash():
        raise ValueError("AWS infrastructure changed since the plan; create a new plan before applying")
    expected = _plan_data(plan)["confirmation_phrase"]
    if confirm != expected:
        return {
            "applied": False,
            "plan_id": plan.plan_id,
            "confirmation_required": expected,
            "message": "Review the cost preview and send this exact phrase to apply the plan.",
        }
    if source_vault is not None:
        source_preflight = _migration_preflight(source_vault)
        if not source_preflight["safe"]:
            raise ValueError(
                "AWS migration is blocked by secret-scan findings; remove them and create a new plan"
            )
        if source_preflight["fingerprint"] != plan.source_fingerprint:
            raise ValueError("vault data changed since the plan; create a new plan before applying")
        if source_vault.name == "sqlite":
            try:
                import boto3  # noqa: F401
            except ImportError as error:
                raise ValueError(
                    "AWS migration needs the optional SDK; install cairn with `pip install 'cairn[aws]'`"
                ) from error
    identity = _aws_identity(plan.profile, plan.region)
    if identity["account"] != plan.account:
        raise ValueError("AWS caller account changed since the plan was created; make a new plan")
    infra = _ensure_cdk()
    env = _command_env(plan.profile, plan.region)
    env["CDK_DEFAULT_ACCOUNT"] = plan.account
    command = ["npm", "run", "synth", "--", *_context_args(plan)]
    result = _run(command, cwd=infra, env=env)
    if result.returncode:
        raise ValueError("local AWS CDK synthesis failed; no AWS resources were changed")
    deploy = ["npm", "exec", "--offline", "--", "cdk", "deploy", plan.stack_name,
              "--require-approval", "never", *_context_args(plan)]
    result = _run(deploy, cwd=infra, env=env)
    if result.returncode:
        raise ValueError("AWS CDK deployment failed; inspect AWS CloudFormation for its status")
    outputs = _stack_outputs(plan)
    migration = None
    if source_vault is not None and source_vault.name == "sqlite":
        from .storage import StorageConfig, open_backend

        config = StorageConfig(
            backend="aws", region=plan.region, profile=plan.profile,
            vault_id=plan.vault_id, vault_name=plan.vault_name,
            table=outputs["MemoryTableName"],
            cache_table=outputs["EmbeddingCacheTableName"],
            content_bucket=outputs["ContentBucketName"],
            vector_bucket=outputs["VectorBucketName"],
            vector_index=outputs["VectorIndexName"],
            vector_index_arn=outputs["VectorIndexArn"],
        )
        destination = open_backend(
            vault_dir, source_vault._embed_name, dimensions,
            config=config, create=True,
        )
        try:
            migration = _migrate_sqlite_to_aws(source_vault, destination)
        finally:
            destination.close()
    config_path = _write_storage_config(vault_dir, plan, outputs)
    return {
        "applied": True, "plan_id": plan.plan_id, "stack_name": plan.stack_name,
        "storage_config": str(config_path), "migration": migration,
    }


def teardown(vault_dir: Path, plan_id: str, confirm: str) -> dict:
    plan = _load_plan(vault_dir, plan_id)
    expected = f"DESTROY {plan.stack_name}"
    if confirm != expected:
        return {
            "destroyed": False,
            "plan_id": plan.plan_id,
            "confirmation_required": expected,
            "message": "The stack retains its vault data. Review the stack before confirming teardown.",
        }
    identity = _aws_identity(plan.profile, plan.region)
    if identity["account"] != plan.account:
        raise ValueError("AWS caller account changed since the plan was created; make a new plan")
    infra = _ensure_cdk()
    env = _command_env(plan.profile, plan.region)
    env["CDK_DEFAULT_ACCOUNT"] = plan.account
    command = ["npm", "exec", "--offline", "--", "cdk", "destroy", plan.stack_name,
               "--force"]
    result = _run(command, cwd=infra, env=env)
    if result.returncode:
        raise ValueError("AWS CDK teardown failed; inspect AWS CloudFormation for its status")
    return {
        "destroyed": True,
        "plan_id": plan.plan_id,
        "stack_name": plan.stack_name,
        "data_retained": True,
    }


def run_action(client, args: dict) -> dict:
    action = args.get("action")
    if action == "check":
        return {"tools": check_tools(), "aws_cli_install": AWS_CLI_INSTALL}
    identity = client.vault.vault_identity
    dimensions = int(client.embedder.dims)
    vault_dir = Path(client.vault.vault_dir)
    if action == "plan":
        preflight = _migration_preflight(client.vault)
        plan = create_plan(
            vault_dir, identity.vault_id, identity.name, dimensions,
            args.get("region", ""), args.get("profile"),
            preflight["fingerprint"], preflight["memory_count"],
            preflight["event_count"],
            client.embedder.name,
            args.get("enable_sync_endpoint", False),
        )
        plan["migration_preview"] = {
            key: value for key, value in preflight.items() if key != "fingerprint"
        }
        return plan
    if action == "apply":
        return apply_plan(
            vault_dir, identity.vault_id, identity.name, dimensions,
            args.get("plan_id", ""), args.get("confirm", ""),
            source_vault=client.vault,
        )
    if action == "teardown":
        return teardown(
            vault_dir, args.get("plan_id", ""), args.get("confirm", ""),
        )
    if action == "status":
        plan = _load_plan(vault_dir, args.get("plan_id", ""))
        identity_result = _aws_identity(plan.profile, plan.region)
        if identity_result["account"] != plan.account:
            raise ValueError("AWS caller account changed since the plan was created")
        command = ["aws", "cloudformation", "describe-stacks", "--stack-name", plan.stack_name,
                   "--region", plan.region, "--output", "json"]
        if plan.profile:
            command.extend(["--profile", plan.profile])
        result = _run(command, cwd=_repo_root(), env=_command_env(plan.profile, plan.region))
        if result.returncode:
            return {"stack_name": plan.stack_name, "deployed": False, "status": "not found"}
        try:
            stack = json.loads(result.stdout)["Stacks"][0]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise ValueError("AWS CLI returned an invalid stack status response") from error
        return {
            "stack_name": plan.stack_name,
            "deployed": True,
            "status": stack.get("StackStatus", "unknown"),
            "outputs": stack.get("Outputs", []),
        }
    raise ValueError("cairn_aws_storage action must be check, plan, apply, status, or teardown")
