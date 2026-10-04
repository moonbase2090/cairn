from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tomllib

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
