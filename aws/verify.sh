#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
infra_dir="$repo_root/aws/infra"

cd "$infra_dir"
npm ci
npm test
npm run build
npm run synth:mock
npm run synth -- --context vaultId=0123456789abcdef0123456789abcdef --context vaultName=local-mock --context dimensions=384 --context region=us-west-2 --context embedModel=hash --context enableSyncEndpoint=true

cd "$repo_root"
uv run --extra dev --extra postgres python -m pytest -o addopts= -q \
	 tests/test_aws_api.py \
	 tests/test_aws_storage.py \
	 tests/test_aws_control.py \
	 tests/test_aws_cleanup.py \
	 tests/test_storage_config.py \
	 tests/test_sync_url.py
