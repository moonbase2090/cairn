---
name: verify-cairn
description: "Drive Cairn's CLI against isolated SQLite vaults to prove initialization, memory storage, document ingest, project onboarding, and portable sync after code changes."
---

# Verify Cairn

Use this skill to prove Cairn through its user-facing CLI. Read
[`features/README.md`](features/README.md) first, then use the matching feature
recipe. Cairn's primary surface is the CLI. The MCP server, self-hosted HTTP
sync server, and local Galaxy HTTP view are secondary surfaces; they need
surface-specific verification when a change touches them.

## Launch

Cairn is a short-lived CLI, not a server that stays open. From the repository
root, build its editable package and confirm the CLI starts:

~~~sh
CAIRN_REPO="$PWD"
uv run --no-project --with-editable "$CAIRN_REPO" cairn --help
~~~

The command exits with status `0` and prints `usage: cairn`. It uses the
project's declared base dependencies and does not install optional extras such
as AWS or fastembed. `--no-project` keeps this check independent of the
repository lockfile. No browser or PTY harness is needed for the primary
surface; drive the same CLI commands a user runs. Each invocation exits after
its command, so no Cairn process remains between local drives.

## Doctor

Before driving a fresh vault, initialize it with the isolated-vault recipe and
run `doctor --json`. Require `storage` to be `sqlite`, `embedder` to be
`hash-v2/384d`, and `memories` to match the known fixture count. Omit
`--repair-vec`, which rebuilds the vector index. Never point this check at a
user vault. Run doctor again after a failed drive before deciding whether to
retry.

## Drive

Run from the checkout root. Give every drive a new temporary directory and
evidence directory. Explicitly select SQLite in the temporary vault before
`init`: Cairn can otherwise inherit a backend from `~/.cairn/config.toml` even
when `--vault` points elsewhere. Use the hash embedder and an `e2e-*` identity
so the drive stays local and its writes are recognizable as disposable.

~~~sh
set -eu
CAIRN_REPO="$PWD"
VERIFY_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
VERIFY_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/cairn-verify.XXXXXX")"
VERIFY_VAULT="$VERIFY_ROOT/vault"
VERIFY_EVIDENCE="${TMPDIR:-/tmp}/cairn-verify-evidence-$VERIFY_ID"
mkdir -p "$VERIFY_VAULT" "$VERIFY_EVIDENCE"
printf '[storage]\nbackend = "sqlite"\n' > "$VERIFY_VAULT/config.toml"

record() {
  local label result_code arg
  label=$1
  shift
  {
    printf '$'
    for arg in "$@"; do printf ' %q' "$arg"; done
    printf '\n'
  } >> "$VERIFY_EVIDENCE/commands.log"
  if "$@" > "$VERIFY_EVIDENCE/$label.stdout" 2> "$VERIFY_EVIDENCE/$label.stderr"; then
    result_code=0
  else
    result_code=$?
  fi
  printf '%s\n' "$result_code" > "$VERIFY_EVIDENCE/$label.exit"
  cat "$VERIFY_EVIDENCE/$label.stdout"
  cat "$VERIFY_EVIDENCE/$label.stderr" >&2
  printf 'exit code: %s\n' "$result_code"
  return "$result_code"
}

record init uv run --no-project --with-editable "$CAIRN_REPO" cairn \
  --vault "$VERIFY_VAULT" --agent-id e2e-verification init --yes \
  --embed-spec hash --project 'Cairn verification' --team verify --json
record doctor uv run --no-project --with-editable "$CAIRN_REPO" cairn \
  --vault "$VERIFY_VAULT" --agent-id e2e-verification doctor --json
~~~

Use the commands in the feature recipe after this baseline. The `record`
function saves each exact invocation, stdout, stderr, and exit code separately.
Feature checks must assert the returned value and confirm persisted state with a
second CLI command. Do not rely on a success message alone.

## Evidence

Capture the user command and its resulting state. Keep `VERIFY_EVIDENCE`
separate from `VERIFY_ROOT`, so cleanup can remove the vault while retaining
the proof. Include the feature ID and entry point in any evidence summary.
CLI proof needs `commands.log`, each command's `.stdout`, `.stderr`, and
`.exit` files, plus the assertion result. Record the assertion with label
`assertion` so cleanup can confirm `assertion.exit` is present. Confirm the
evidence directory still exists after cleanup. Do not commit local proof
output unless a PR specifically needs an artifact and the repository policy
calls for it.

Use real CLI commands for the behavior under review. Local fixtures may seed
temporary files, but do not replace Cairn's command with an internal setter or
call an implementation function directly. Keep cloud and service boundaries
local or mocked; this default SQLite drive must not contact AWS or a sync peer.

## Cleanup

The mapped local CLI drives start no long-lived process. After a successful or
failed iteration, remove only the `VERIFY_ROOT` created by that iteration. Use
Python's `shutil.rmtree` after checking that the resolved target is a direct
child of the system temporary directory and its name begins with
`cairn-verify.`. Never remove `VERIFY_EVIDENCE`. If a separate feature starts a
server, stop only the PID started by that drive, then remove its temporary
state. Never kill processes by name. After cleanup, verify the evidence files
remain at `VERIFY_EVIDENCE`.

~~~sh
python3 - "$VERIFY_ROOT" <<'PY'
import os
import pathlib
import shutil
import tempfile
import sys

target = pathlib.Path(sys.argv[1])
temp_root = pathlib.Path(os.environ.get("TMPDIR", tempfile.gettempdir())).resolve()
if target.exists():
    assert target.name.startswith("cairn-verify."), target
    assert target.resolve().parent == temp_root, (target, temp_root)
    shutil.rmtree(target)
else:
    assert not target.is_symlink(), target
PY
test -d "$VERIFY_EVIDENCE"
test -f "$VERIFY_EVIDENCE/assertion.exit"
~~~

## Helpers

There are no repository helper scripts. The inline `record` function above is
the only shell helper; keep its invocation and behavior in this skill if it
changes. Use `python3` for standard-library JSON assertions and guarded cleanup.
