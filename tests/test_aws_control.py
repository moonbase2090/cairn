from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tomllib
from types import SimpleNamespace

import pytest

from cairn import aws_control
from cairn.mcp_server import TOOL_DEFS, call_tool


VAULT_ID = "0123456789abcdef0123456789abcdef"


def _make_plan(tmp_path: Path, monkeypatch, account: str = "123456789012",
               enable_sync_endpoint: bool = False) -> dict:
    monkeypatch.setattr(
        aws_control, "_aws_identity",
        lambda _profile, _region: {"account": account, "arn": f"arn:aws:iam::{account}:user/test"},
    )
    monkeypatch.setattr(aws_control, "_ensure_cdk", lambda: tmp_path)
    monkeypatch.setattr(aws_control, "_stack_outputs", lambda _plan: {
        "VaultId": VAULT_ID, "VaultName": "work-vault",
        "MemoryTableName": "cairn-memory", "EmbeddingCacheTableName": "cairn-cache",
        "ContentBucketName": "cairn-content", "VectorBucketName": "cairn-vectors",
        "VectorIndexName": "cairn-index",
        "VectorIndexArn": "arn:aws:s3vectors:us-west-2:123456789012:index/mock",
    })
    monkeypatch.setattr(aws_control, "_synthesize", lambda _plan, _infra: None)
    return aws_control.create_plan(
        tmp_path, VAULT_ID, "work-vault", 384, "us-west-2", "work",
        enable_sync_endpoint=enable_sync_endpoint,
    )


def test_plan_is_local_synth_only_and_uses_published_pricing(tmp_path, monkeypatch):
    plan = _make_plan(tmp_path, monkeypatch)

    assert plan["synthesis"].startswith("complete")
    assert plan["estimate"] is None
    assert {row["service"] for row in plan["pricing_preview"]} == {
        "S3", "S3 Vectors", "DynamoDB", "KMS", "Lambda", "CloudWatch", "EventBridge",
    }
    assert all(row["url"].startswith("https://aws.amazon.com/") for row in plan["pricing_preview"])
    saved = aws_control._plan_path(tmp_path, plan["plan_id"])
    assert saved.exists()
    assert saved.stat().st_mode & 0o777 == 0o600


def test_sync_api_is_opt_in_and_adds_gateway_cost_to_plan(tmp_path, monkeypatch):
    disabled = _make_plan(tmp_path / "disabled", monkeypatch)
    enabled = _make_plan(tmp_path / "enabled", monkeypatch, enable_sync_endpoint=True)

    assert disabled["enable_sync_endpoint"] is False
    assert "API Gateway" not in {row["service"] for row in disabled["pricing_preview"]}
    assert enabled["enable_sync_endpoint"] is True
    assert "API Gateway" in {row["service"] for row in enabled["pricing_preview"]}
    assert disabled["plan_id"] != enabled["plan_id"]
    saved = json.loads(
        aws_control._plan_path(tmp_path / "enabled", enabled["plan_id"]).read_text(),
    )
    assert "enableSyncEndpoint=true" in aws_control._context_args(
        aws_control.AwsPlan(**saved),
    )
    config_path = aws_control._write_storage_config(
        tmp_path / "enabled-vault",
        aws_control.AwsPlan(**saved),
        {
            "MemoryTableName": "memory", "EmbeddingCacheTableName": "cache",
            "ContentBucketName": "content", "VectorBucketName": "vectors",
            "VectorIndexName": "index",
            "VectorIndexArn": "arn:aws:s3vectors:us-west-2:123456789012:index/mock",
            "SyncApiUrl": "https://abc123.execute-api.us-west-2.amazonaws.com",
        },
    )
    assert tomllib.loads(config_path.read_text())["storage"]["sync_endpoint"] == (
        "https://abc123.execute-api.us-west-2.amazonaws.com"
    )


def test_plan_rejects_unbounded_or_controlled_embed_model(tmp_path, monkeypatch):
    monkeypatch.setattr(
        aws_control, "_aws_identity",
        lambda _profile, _region: {"account": "123456789012", "arn": "arn:test"},
    )
    monkeypatch.setattr(aws_control, "_ensure_cdk", lambda: tmp_path)
    monkeypatch.setattr(aws_control, "_synthesize", lambda _plan, _infra: None)

    for embed_model in ("x" * 257, "local\nmalicious"):
        try:
            aws_control.create_plan(
                tmp_path, VAULT_ID, "work-vault", 384, "us-west-2", embed_model=embed_model,
            )
        except ValueError as error:
            assert "printable characters" in str(error)
        else:
            raise AssertionError("invalid embed model was accepted")


def test_plan_uses_configured_region_when_no_override_is_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        aws_control, "_aws_identity",
        lambda _profile, region: {
            "account": "123456789012", "arn": "arn:aws:iam::123456789012:user/test",
        } if region == "eu-west-1" else (_ for _ in ()).throw(AssertionError(region)),
    )
    monkeypatch.setattr(aws_control, "_configured_region", lambda _profile: "eu-west-1")
    monkeypatch.setattr(aws_control, "_ensure_cdk", lambda: tmp_path)
    monkeypatch.setattr(aws_control, "_synthesize", lambda _plan, _infra: None)

    plan = aws_control.create_plan(tmp_path, VAULT_ID, "work-vault", 384, "")

    assert plan["region"] == "eu-west-1"


def test_account_904_requires_its_explicit_confirmation_and_apply_is_mocked(
    tmp_path, monkeypatch,
):
    plan = _make_plan(tmp_path, monkeypatch, "904233124492")
    calls: list[list[str]] = []

    monkeypatch.setattr(
        aws_control, "_aws_identity",
        lambda _profile, _region: {
            "account": "904233124492", "arn": "arn:aws:iam::904233124492:user/test",
        },
    )
    monkeypatch.setattr(aws_control, "_ensure_cdk", lambda: tmp_path)
    monkeypatch.setattr(
        aws_control, "_run",
        lambda command, **_kwargs: calls.append(command)
        or subprocess.CompletedProcess(command, 0, "", ""),
    )

    prompt = aws_control.apply_plan(
        tmp_path, VAULT_ID, "work-vault", 384, plan["plan_id"],
        f"DEPLOY {plan['plan_id']}",
    )
    assert prompt["confirmation_required"] == f"DEPLOY MB2090 ACCOUNT {plan['plan_id']}"
    assert calls == []

    applied = aws_control.apply_plan(
        tmp_path, VAULT_ID, "work-vault", 384, plan["plan_id"],
        f"DEPLOY MB2090 ACCOUNT {plan['plan_id']}",
    )
    assert applied["applied"] is True
    config_path = Path(applied["storage_config"])
    assert config_path.read_text().startswith("[storage]\nbackend = \"aws\"")
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert len(calls) == 2
    assert "synth" in calls[0]
    assert "deploy" in calls[1]
    assert "vaultId=" + VAULT_ID in calls[1]


def test_teardown_requires_exact_confirmation_before_any_cli_call(tmp_path, monkeypatch):
    plan = _make_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(
        aws_control, "_aws_identity",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not reach AWS")),
    )

    result = aws_control.teardown(tmp_path, plan["plan_id"], "")

    assert result["destroyed"] is False
    assert result["confirmation_required"] == f"DESTROY {plan['stack_name']}"


def test_mcp_aws_setup_tool_uses_cli_profiles_without_secret_fields(monkeypatch):
    tool = next(tool for tool in TOOL_DEFS if tool["name"] == "cairn_aws_storage")
    assert "region" in tool["inputSchema"]["properties"]
    assert "access_key" not in tool["inputSchema"]["properties"]
    assert "secret_key" not in tool["inputSchema"]["properties"]
    assert tool["inputSchema"]["properties"]["enable_sync_endpoint"]["default"] is False

    monkeypatch.setattr(aws_control, "check_tools", lambda: {"aws": "available"})
    assert '"aws": "available"' in call_tool(object(), "cairn_aws_storage", {"action": "check"})


def _aws_plan(**overrides) -> aws_control.AwsPlan:
    values = {
        "plan_id": "a" * 16, "account": "123456789012", "region": "us-west-2",
        "profile": "work", "vault_id": VAULT_ID, "vault_name": "work-vault",
        "dimensions": 3, "stack_name": "CairnVault-test", "infra_hash": "infra-hash",
        "created_at": "2026-10-04T00:00:00+00:00", "enable_sync_endpoint": False,
    }
    return aws_control.AwsPlan(**(values | overrides))


def _stack_outputs(sync_endpoint: str | None = None) -> dict[str, str]:
    outputs = {
        "VaultId": VAULT_ID, "VaultName": "work-vault",
        "MemoryTableName": "memory", "EmbeddingCacheTableName": "cache",
        "ContentBucketName": "content", "VectorBucketName": "vectors",
        "VectorIndexName": "index",
        "VectorIndexArn": "arn:aws:s3vectors:us-west-2:123456789012:index/mock",
    }
    if sync_endpoint is not None:
        outputs["SyncApiUrl"] = sync_endpoint
    return outputs


def test_aws_identity_validates_the_mocked_cli_response_and_adds_profile(monkeypatch):
    calls = []
    result = subprocess.CompletedProcess(
        ["aws"], 0, json.dumps({"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/test"}), "",
    )
    monkeypatch.setattr(aws_control.shutil, "which", lambda name: "/mock/aws" if name == "aws" else None)
    monkeypatch.setattr(aws_control, "_run", lambda command, **_kwargs: calls.append(command) or result)

    identity = aws_control._aws_identity("test-profile", "eu-west-1")

    assert identity == {"account": "123456789012", "arn": "arn:aws:iam::123456789012:user/test"}
    assert calls == [[
        "aws", "sts", "get-caller-identity", "--output", "json", "--region",
        "eu-west-1", "--profile", "test-profile",
    ]]


@pytest.mark.parametrize(
    "returncode,stdout,message",
    [
        (1, "", "could not verify"),
        (0, "not-json", "invalid caller identity"),
        (0, '{"Account":"bad","Arn":"arn:test"}', "valid account ID"),
        (0, '{"Account":"123456789012","Arn":"invalid"}', "include an ARN"),
    ],
)
def test_aws_identity_rejects_failed_or_malformed_mocked_cli_results(
    monkeypatch, returncode, stdout, message,
):
    monkeypatch.setattr(aws_control.shutil, "which", lambda _name: "/mock/aws")
    monkeypatch.setattr(
        aws_control, "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, returncode, stdout, ""),
    )

    with pytest.raises(ValueError, match=message):
        aws_control._aws_identity(None, "us-west-2")


def test_aws_identity_reports_missing_cli_without_running_a_command(monkeypatch):
    monkeypatch.setattr(aws_control.shutil, "which", lambda _name: None)
    monkeypatch.setattr(aws_control, "_run", lambda *_args, **_kwargs: pytest.fail("CLI must not run"))

    with pytest.raises(ValueError, match="AWS CLI is required"):
        aws_control._aws_identity(None, "us-west-2")


def test_check_tools_reports_missing_versions_and_local_cdk(monkeypatch, tmp_path):
    infra = tmp_path / "aws" / "infra"
    cdk = infra / "node_modules" / ".bin" / "cdk"
    cdk.parent.mkdir(parents=True)
    cdk.write_text("mock executable")
    monkeypatch.setattr(aws_control, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        aws_control.shutil, "which",
        lambda name: f"/mock/{name}" if name in {"aws", "node", "npm"} else None,
    )
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        output = "v2.1.0\n" if command[-1] == "--version" and command[0] == str(cdk) else "v1.0\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(aws_control, "_run", run)

    versions = aws_control.check_tools()

    assert versions == {"aws": "v1.0", "node": "v1.0", "npm": "v1.0", "cdk": "v2.1.0"}
    assert calls[-1] == [str(cdk), "--version"]


def test_check_tools_skips_missing_binaries_and_reports_missing_cdk(monkeypatch, tmp_path):
    monkeypatch.setattr(aws_control, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        aws_control.shutil, "which", lambda name: "/mock/node" if name == "node" else None,
    )
    monkeypatch.setattr(
        aws_control, "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", "failure"),
    )

    assert aws_control.check_tools() == {
        "aws": "missing", "node": "unavailable", "npm": "missing",
        "cdk": "needs local npm install",
    }


def test_configured_region_prefers_environment_and_handles_missing_cli(monkeypatch):
    monkeypatch.setenv("AWS_REGION", " eu-west-1 ")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setattr(aws_control.shutil, "which", lambda _name: None)
    assert aws_control._configured_region(None) == "eu-west-1"

    monkeypatch.delenv("AWS_REGION")
    assert aws_control._configured_region(None) == "us-west-2"
    monkeypatch.delenv("AWS_DEFAULT_REGION")
    assert aws_control._configured_region(None) is None


def test_configured_region_validates_profile_then_reads_mocked_aws_config(monkeypatch):
    monkeypatch.setattr(aws_control.shutil, "which", lambda _name: "/mock/aws")
    calls = []
    monkeypatch.setattr(
        aws_control, "_run",
        lambda command, **_kwargs: calls.append(command)
        or subprocess.CompletedProcess(command, 0, "eu-central-1\n", ""),
    )

    with pytest.raises(ValueError, match="profile name"):
        aws_control._configured_region("bad profile")
    assert aws_control._configured_region("work") == "eu-central-1"
    assert calls == [["aws", "configure", "get", "region", "--profile", "work"]]


def test_configured_region_returns_none_for_empty_cli_output(monkeypatch):
    monkeypatch.setattr(aws_control.shutil, "which", lambda _name: "/mock/aws")
    monkeypatch.setattr(
        aws_control, "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", "not configured"),
    )

    assert aws_control._configured_region(None) is None


def test_ensure_cdk_reuses_matching_lock_marker_without_installing(monkeypatch, tmp_path):
    infra = tmp_path / "aws" / "infra"
    node_modules = infra / "node_modules"
    cdk = node_modules / ".bin" / "cdk"
    cdk.parent.mkdir(parents=True)
    cdk.write_text("mock executable")
    (infra / "package-lock.json").write_text("lock")
    lock_hash = aws_control.hashlib.sha256(b"lock").hexdigest()
    (node_modules / ".cairn-lock-hash").write_text(lock_hash)
    monkeypatch.setattr(aws_control, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(aws_control.shutil, "which", lambda _name: "/mock/npm")
    monkeypatch.setattr(aws_control, "_run", lambda *_args, **_kwargs: pytest.fail("install is unnecessary"))

    assert aws_control._ensure_cdk() == infra


def test_ensure_cdk_installs_pinned_dependencies_when_marker_is_stale(monkeypatch, tmp_path):
    infra = tmp_path / "aws" / "infra"
    infra.mkdir(parents=True)
    (infra / "package-lock.json").write_text("new lock")
    monkeypatch.setattr(aws_control, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(aws_control.shutil, "which", lambda _name: "/mock/npm")
    calls = []
    monkeypatch.setattr(
        aws_control, "_run",
        lambda command, **_kwargs: calls.append(command)
        or subprocess.CompletedProcess(command, 0, "", ""),
    )

    assert aws_control._ensure_cdk() == infra
    assert calls == [["npm", "ci"]]
    marker = infra / "node_modules" / ".cairn-lock-hash"
    assert marker.read_text().strip() == aws_control.hashlib.sha256(b"new lock").hexdigest()


def test_ensure_cdk_reports_missing_npm_and_install_failure(monkeypatch, tmp_path):
    infra = tmp_path / "aws" / "infra"
    infra.mkdir(parents=True)
    (infra / "package-lock.json").write_text("lock")
    monkeypatch.setattr(aws_control, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(aws_control.shutil, "which", lambda _name: None)
    with pytest.raises(ValueError, match="Node.js and npm"):
        aws_control._ensure_cdk()

    monkeypatch.setattr(aws_control.shutil, "which", lambda _name: "/mock/npm")
    monkeypatch.setattr(
        aws_control, "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", "failed"),
    )
    with pytest.raises(ValueError, match="could not install"):
        aws_control._ensure_cdk()


def test_stack_outputs_validates_mocked_outputs_and_sync_endpoint(monkeypatch):
    plan = _aws_plan(enable_sync_endpoint=True)
    calls = []
    monkeypatch.setattr(
        aws_control, "_run",
        lambda command, **_kwargs: calls.append(command)
        or subprocess.CompletedProcess(command, 0, json.dumps({"Stacks": [{
            "Outputs": [{"OutputKey": key, "OutputValue": value}
                        for key, value in _stack_outputs("https://api.example").items()],
        }]}), ""),
    )

    assert aws_control._stack_outputs(plan) == _stack_outputs("https://api.example")
    assert calls[0][-2:] == ["--profile", "work"]


@pytest.mark.parametrize(
    "returncode,stdout,expected",
    [
        (1, "", "could not be read"),
        (0, "{}", "invalid stack output"),
        (0, json.dumps({"Stacks": [{"Outputs": []}]}), "missing outputs"),
        (0, json.dumps({"Stacks": [{"Outputs": [
            {"OutputKey": key, "OutputValue": value}
            for key, value in (_stack_outputs() | {"VaultId": "f" * 32}).items()
        ]}]}), "do not match"),
    ],
)
def test_stack_outputs_rejects_failed_malformed_or_mismatched_mocked_responses(
    monkeypatch, returncode, stdout, expected,
):
    monkeypatch.setattr(
        aws_control, "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, returncode, stdout, ""),
    )

    with pytest.raises(ValueError, match=expected):
        aws_control._stack_outputs(_aws_plan())


def test_stack_outputs_requires_and_validates_sync_url_when_enabled(monkeypatch):
    outputs = _stack_outputs()
    monkeypatch.setattr(
        aws_control, "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, json.dumps({
            "Stacks": [{"Outputs": [{"OutputKey": key, "OutputValue": value}
                                    for key, value in outputs.items()]}],
        }), ""),
    )
    with pytest.raises(ValueError, match="missing outputs"):
        aws_control._stack_outputs(_aws_plan(enable_sync_endpoint=True))

    outputs["SyncApiUrl"] = "http://not-tls.example"
    monkeypatch.setattr(
        aws_control, "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, json.dumps({
            "Stacks": [{"Outputs": [{"OutputKey": key, "OutputValue": value}
                                    for key, value in outputs.items()]}],
        }), ""),
    )
    with pytest.raises(ValueError, match="invalid sync API endpoint"):
        aws_control._stack_outputs(_aws_plan(enable_sync_endpoint=True))


def _prepared_apply(tmp_path, monkeypatch, *, run_results=None):
    plan_data = _make_plan(tmp_path, monkeypatch)
    plan = aws_control._load_plan(tmp_path, plan_data["plan_id"])
    monkeypatch.setattr(aws_control, "_infra_hash", lambda: plan.infra_hash)
    monkeypatch.setattr(
        aws_control, "_aws_identity",
        lambda _profile, _region: {"account": plan.account, "arn": f"arn:aws:iam::{plan.account}:role/test"},
    )
    monkeypatch.setattr(aws_control, "_ensure_cdk", lambda: tmp_path)
    monkeypatch.setattr(aws_control, "_stack_outputs", lambda _plan: _stack_outputs())
    responses = list(run_results or [0, 0])
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        code = responses.pop(0)
        return subprocess.CompletedProcess(command, code, "", "mock failure" if code else "")

    monkeypatch.setattr(aws_control, "_run", run)
    confirmation = aws_control._plan_data(plan)["confirmation_phrase"]
    return plan_data, plan, confirmation, calls


def test_apply_plan_rejects_vault_or_infrastructure_changes(tmp_path, monkeypatch):
    plan_data, _plan, confirmation, _calls = _prepared_apply(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="does not match"):
        aws_control.apply_plan(
            tmp_path, "f" * 32, "work-vault", 384, plan_data["plan_id"], confirmation,
        )

    monkeypatch.setattr(aws_control, "_infra_hash", lambda: "changed")
    with pytest.raises(ValueError, match="infrastructure changed"):
        aws_control.apply_plan(
            tmp_path, VAULT_ID, "work-vault", 384, plan_data["plan_id"], confirmation,
        )


@pytest.mark.parametrize(
    "preflight,expected",
    [
        ({"safe": False, "fingerprint": "source"}, "blocked by secret-scan"),
        ({"safe": True, "fingerprint": "changed"}, "data changed since the plan"),
    ],
)
def test_apply_plan_checks_sqlite_preflight_and_fingerprint(tmp_path, monkeypatch, preflight, expected):
    plan_data, _plan, confirmation, calls = _prepared_apply(tmp_path, monkeypatch)
    source = SimpleNamespace(name="sqlite")
    monkeypatch.setattr(aws_control, "_migration_preflight", lambda _source: preflight)

    with pytest.raises(ValueError, match=expected):
        aws_control.apply_plan(
            tmp_path, VAULT_ID, "work-vault", 384, plan_data["plan_id"], confirmation,
            source_vault=source,
        )

    assert calls == []


def test_apply_plan_rejects_changed_aws_account(tmp_path, monkeypatch):
    plan_data, plan, confirmation, calls = _prepared_apply(tmp_path, monkeypatch)
    monkeypatch.setattr(
        aws_control, "_aws_identity", lambda *_args: {"account": "999999999999", "arn": "arn:test"},
    )

    with pytest.raises(ValueError, match="account changed"):
        aws_control.apply_plan(
            tmp_path, VAULT_ID, "work-vault", 384, plan_data["plan_id"], confirmation,
        )

    assert calls == []
    assert plan.account == "123456789012"


@pytest.mark.parametrize(
    "run_results,expected",
    [([1], "synthesis failed"), ([0, 1], "deployment failed")],
)
def test_apply_plan_never_executes_unmocked_aws_commands_on_failures(
    tmp_path, monkeypatch, run_results, expected,
):
    plan_data, _plan, confirmation, calls = _prepared_apply(
        tmp_path, monkeypatch, run_results=run_results,
    )

    with pytest.raises(ValueError, match=expected):
        aws_control.apply_plan(
            tmp_path, VAULT_ID, "work-vault", 384, plan_data["plan_id"], confirmation,
        )

    assert len(calls) == len(run_results)
    assert all(command[0] == "npm" for command in calls)


def test_apply_plan_migrates_sqlite_only_after_mocked_synthesis_and_deploy(tmp_path, monkeypatch):
    plan_data, _plan, confirmation, calls = _prepared_apply(tmp_path, monkeypatch)
    source = SimpleNamespace(name="sqlite", _embed_name="hash")
    destination = SimpleNamespace(close=lambda: calls.append(["destination.close"]))
    monkeypatch.setattr(
        aws_control, "_migration_preflight",
        lambda _source: {"safe": True, "fingerprint": "", "memory_count": 0, "event_count": 0},
    )
    monkeypatch.setattr(
        "cairn.storage.open_backend",
        lambda *args, **kwargs: calls.append(["open_backend", *args[1:]]) or destination,
    )
    monkeypatch.setattr(
        aws_control, "_migrate_sqlite_to_aws",
        lambda _source, _destination: {"memories": 0, "events": 0},
    )

    result = aws_control.apply_plan(
        tmp_path, VAULT_ID, "work-vault", 384, plan_data["plan_id"], confirmation,
        source_vault=source,
    )

    assert result["applied"] is True
    assert result["migration"] == {"memories": 0, "events": 0}
    assert [command[1] for command in calls[:2]] == ["run", "exec"]
    assert calls[-1] == ["destination.close"]


def _client_for_action(tmp_path):
    vault = SimpleNamespace(
        vault_identity=SimpleNamespace(vault_id=VAULT_ID, name="work-vault"),
        vault_dir=tmp_path,
        name="sqlite",
    )
    return SimpleNamespace(vault=vault, embedder=SimpleNamespace(dims=384, name="hash"))


def test_run_action_plan_returns_preflight_summary_without_fingerprint(tmp_path, monkeypatch):
    client = _client_for_action(tmp_path)
    preflight = {"safe": True, "fingerprint": "private", "memory_count": 2, "event_count": 3}
    monkeypatch.setattr(aws_control, "_migration_preflight", lambda _vault: preflight)
    monkeypatch.setattr(
        aws_control, "create_plan",
        lambda *args: {"plan_id": "plan", "region": args[4]},
    )

    result = aws_control.run_action(client, {"action": "plan", "region": "us-west-2"})

    assert result["region"] == "us-west-2"
    assert result["migration_preview"] == {"safe": True, "memory_count": 2, "event_count": 3}
    assert "fingerprint" not in result["migration_preview"]


def test_run_action_apply_and_teardown_forward_exact_confirmation(tmp_path, monkeypatch):
    client = _client_for_action(tmp_path)
    calls = []
    monkeypatch.setattr(
        aws_control, "apply_plan",
        lambda *args, **kwargs: calls.append(("apply", args, kwargs)) or {"applied": True},
    )
    monkeypatch.setattr(
        aws_control, "teardown",
        lambda *args: calls.append(("teardown", args)) or {"destroyed": True},
    )

    assert aws_control.run_action(client, {
        "action": "apply", "plan_id": "a" * 16, "confirm": "exact",
    }) == {"applied": True}
    assert aws_control.run_action(client, {
        "action": "teardown", "plan_id": "a" * 16, "confirm": "destroy",
    }) == {"destroyed": True}
    assert calls[0][1][4:6] == ("a" * 16, "exact")
    assert calls[0][2]["source_vault"] is client.vault
    assert calls[1][1] == (tmp_path, "a" * 16, "destroy")


@pytest.mark.parametrize("stack_result,expected", [
    (subprocess.CompletedProcess(["aws"], 1, "", "missing"), {"deployed": False, "status": "not found"}),
    (subprocess.CompletedProcess(["aws"], 0, json.dumps({"Stacks": [{
        "StackStatus": "CREATE_COMPLETE", "Outputs": [{"OutputKey": "VaultId", "OutputValue": VAULT_ID}],
    }]}), ""), {"deployed": True, "status": "CREATE_COMPLETE"}),
])
def test_run_action_status_uses_mocked_cloudformation_output(tmp_path, monkeypatch, stack_result, expected):
    client = _client_for_action(tmp_path)
    plan = _aws_plan()
    calls = []
    monkeypatch.setattr(aws_control, "_load_plan", lambda *_args: plan)
    monkeypatch.setattr(
        aws_control, "_aws_identity", lambda *_args: {"account": plan.account, "arn": "arn:test"},
    )
    monkeypatch.setattr(
        aws_control, "_run", lambda command, **_kwargs: calls.append(command) or stack_result,
    )

    result = aws_control.run_action(client, {"action": "status", "plan_id": plan.plan_id})

    assert {key: result[key] for key in expected} == expected
    assert calls == [[
        "aws", "cloudformation", "describe-stacks", "--stack-name", plan.stack_name,
        "--region", plan.region, "--output", "json", "--profile", "work",
    ]]


def test_run_action_status_rejects_account_and_invalid_json(tmp_path, monkeypatch):
    client = _client_for_action(tmp_path)
    plan = _aws_plan()
    monkeypatch.setattr(aws_control, "_load_plan", lambda *_args: plan)
    monkeypatch.setattr(
        aws_control, "_aws_identity", lambda *_args: {"account": "999999999999", "arn": "arn:test"},
    )
    with pytest.raises(ValueError, match="account changed"):
        aws_control.run_action(client, {"action": "status", "plan_id": plan.plan_id})

    monkeypatch.setattr(
        aws_control, "_aws_identity", lambda *_args: {"account": plan.account, "arn": "arn:test"},
    )
    monkeypatch.setattr(
        aws_control, "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "not-json", ""),
    )
    with pytest.raises(ValueError, match="invalid stack status"):
        aws_control.run_action(client, {"action": "status", "plan_id": plan.plan_id})


def test_run_action_rejects_unknown_action(tmp_path):
    with pytest.raises(ValueError, match="action must be"):
        aws_control.run_action(_client_for_action(tmp_path), {"action": "unknown"})
