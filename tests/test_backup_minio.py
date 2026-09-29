"""End-to-end WAL backup, target fan-out, and point-in-time recovery on MinIO."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import boto3
import pytest


ENDPOINT = os.environ.get("CAIRN_TEST_MINIO_ENDPOINT")
LITESTREAM = shutil.which("litestream")
pytestmark = pytest.mark.skipif(
    not ENDPOINT or not LITESTREAM,
    reason="set CAIRN_TEST_MINIO_ENDPOINT and install Litestream to run MinIO backup coverage",
)


def _run(vault: Path, home: Path, *args: str, env: dict[str, str]) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "cairn.cli", "--vault", str(vault), "--agent-id", "minio-test",
         *args, "--json"],
        env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    if result.returncode:
        raise AssertionError(f"Cairn command failed: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout) if result.stdout.strip().startswith(("{", "[")) else {"text": result.stdout}


def _wait_for_sync(vault: Path, home: Path, env: dict[str, str], target_names: list[str], previous: str | None = None) -> str:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        result = _run(vault, home, "backup", "status", env=env)
        targets = {target["name"]: target for target in result["targets"]}
        if all(name in targets and targets[name]["last_success"] for name in target_names):
            times = [targets[name]["last_success"] for name in target_names]
            if previous is None or all(value > previous for value in times):
                return min(times)
        if any(targets.get(name, {}).get("last_error") for name in target_names):
            errors = {name: targets[name]["last_error"] for name in target_names if targets.get(name, {}).get("last_error")}
            raise AssertionError(f"backup replication failed: {errors}")
        time.sleep(0.25)
    raise AssertionError("backup targets did not synchronize before the deadline")


def test_minio_and_local_targets_restore_a_point_in_time(tmp_path, monkeypatch):
    endpoint = ENDPOINT or ""
    host = urlsplit(endpoint).hostname
    assert host in {"localhost", "127.0.0.1", "::1"}, "MinIO tests must use a local endpoint"

    bucket = f"cairn-test-{uuid.uuid4().hex[:10]}"
    prefix = f"cairn/{uuid.uuid4().hex}"
    region = "us-east-1"
    access_key = os.environ.get("CAIRN_TEST_MINIO_ACCESS_KEY", "minioadmin")
    secret_key = os.environ.get("CAIRN_TEST_MINIO_SECRET_KEY", "minioadmin")
    client = boto3.client(
        "s3", endpoint_url=endpoint, region_name=region,
        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
    )
    deadline = time.monotonic() + 30
    while True:
        try:
            client.create_bucket(Bucket=bucket)
            break
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)

    vault = tmp_path / "project" / ".cairn"
    home = tmp_path / "home"
    home.mkdir()
    folder_target = tmp_path / "file-backup"
    config_path = home / ".cairn" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        "[[backup]]\n"
        'name = "minio"\n'
        'kind = "s3"\n'
        f'bucket = "{bucket}"\n'
        f'endpoint = "{endpoint}"\n'
        f'path = "{prefix}"\n'
        f'region = "{region}"\n'
        "sync_interval = 0.5\n"
        'snapshot_interval = "1s"\n'
        'retention = "168h"\n'
        'l0_retention = "24h"\n\n'
        "[[backup]]\n"
        'name = "local"\n'
        'kind = "file"\n'
        f'path = "{folder_target}"\n'
        "sync_interval = 0.5\n"
        'snapshot_interval = "1s"\n'
        'retention = "168h"\n'
        'l0_retention = "24h"\n'
    )

    env = os.environ.copy()
    env.update(
        HOME=str(home),
        PYTHONUNBUFFERED="1",
        AWS_EC2_METADATA_DISABLED="true",
        AWS_ACCESS_KEY_ID=access_key,
        AWS_SECRET_ACCESS_KEY=secret_key,
        AWS_REGION=region,
        AWS_DEFAULT_REGION=region,
        CAIRN_EMBEDD="0",
        PATH=f"{Path(LITESTREAM or '').parent}{os.pathsep}{env['PATH']}",
    )
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    _run(vault, home, "init", "--embed-spec", "hash", "--yes", env=env)
    first = _run(vault, home, "store", "The violet comet has a 12-month orbit.",
                 "--team", "backup-tests", "--task", "minio-check", "--type", "semantic", env=env)
    large_content = "The content-addressed backup marker is here. " + "orchid signal " * 190
    large = _run(vault, home, "store", large_content,
                 "--team", "backup-tests", "--task", "minio-large-check", "--type", "semantic", env=env)
    assert large["action"] in {"created", "updated"}
    assert len(large_content.encode("utf-8")) > 2048
    runner = subprocess.Popen(
        [sys.executable, "-m", "cairn.cli", "--vault", str(vault), "backup", "replicate"],
        env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        first_sync = _wait_for_sync(vault, home, env, ["minio", "local"])
        first_status = _run(vault, home, "backup", "status", env=env)
        first_snapshot = next(target["last_snapshot"] for target in first_status["targets"]
                              if target["name"] == "minio")
        before = _run(vault, home, "retrieve", "violet comet", env=env)
        before_keys = [memory["key"] for memory in before]
        before_large = _run(vault, home, "retrieve", "content-addressed backup marker", env=env)
        assert first["action"] in {"created", "updated"}
        assert before_keys

        time.sleep(2)
        _run(vault, home, "store", "The amber moon has a 30-day cycle.",
             "--team", "backup-tests", "--task", "minio-check", "--type", "semantic", env=env)
        later_sync = _wait_for_sync(vault, home, env, ["minio", "local"], previous=first_sync)
        assert later_sync > first_sync
        later_status = _run(vault, home, "backup", "status", env=env)
        later_snapshot = next(target["last_snapshot"] for target in later_status["targets"]
                              if target["name"] == "minio")
        assert later_snapshot > first_snapshot
    finally:
        runner.terminate()
        try:
            runner.wait(timeout=10)
        except subprocess.TimeoutExpired:
            runner.kill()
            runner.wait(timeout=5)
    if runner.returncode not in {0, -15}:
        raise AssertionError(f"backup runner exited with {runner.returncode}: {runner.stdout.read()}")

    shutil.rmtree(vault)
    restored = _run(vault, home, "restore", "--from", "minio", "--at", first_sync, env=env)
    after = _run(vault, home, "retrieve", "violet comet", env=env)
    after_large = _run(vault, home, "retrieve", "content-addressed backup marker", env=env)

    assert restored["target"] == "minio"
    assert [memory["key"] for memory in after] == before_keys
    assert [memory["key"] for memory in after_large] == [memory["key"] for memory in before_large]
    assert after_large[0]["content"] == large_content
    assert len(list(folder_target.rglob("*.ltx"))) >= 2
    assert list((folder_target / "cairn-docs").rglob("*.md"))
    doc_keys = client.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/cairn-docs/")["Contents"]
    assert len(doc_keys) == 1
