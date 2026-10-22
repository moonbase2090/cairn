# Share a portable pack

Portable export and import move a JSON snapshot between Cairn replicas or through a version-controlled file. Import adds missing keys and leaves existing rows unchanged.

## Sub-features

- `pack-export` writes the current memory snapshot to a JSON file.
- `pack-import` adds the pack's missing memories to a compatible vault.
- `pack-repeat` skips already-present keys on a repeated import.
- `pack-readback` makes imported content available from normal Cairn reads.

## How to get to it (user POV)

- Run `cairn export --out <path>` to write a snapshot.
- Run `cairn import <path>` to merge it into another replica.
- Run `cairn list --task <task>` to inspect imported memories.

## Driving it with the Cairn CLI

Preconditions:

- Follow the baseline in `../SKILL.md`, then set `VERIFY_SOURCE="$VERIFY_VAULT"`, `VERIFY_REPLICA="$VERIFY_ROOT/replica"`, and `VERIFY_PACK="$VERIFY_EVIDENCE/portable-pack.json"`.
- The baseline initializes an empty source vault. Copy its closed directory to the replica before either copy stores fixture data; this gives the local fixture the same logical `vault_id`.

- **Prepare the replica fixture.** After the baseline init and doctor commands exit, run `python3 - "$VERIFY_SOURCE" "$VERIFY_REPLICA"` with a script body that imports `pathlib`, `shutil`, and `sys`, then calls `shutil.copytree(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]))`. Run `record source-identity uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_SOURCE" --agent-id e2e-verification whoami --json` and the same command with `--vault "$VERIFY_REPLICA"`; assert the `vault_id` values match. Run `record source-doctor uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_SOURCE" --agent-id e2e-verification doctor --json` and `record replica-doctor uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_REPLICA" --agent-id e2e-verification doctor --json`; assert their `vault` paths differ. Use this copied fixture only for insert-only file-pack checks; never for concurrent writes or event sync.
- **Store on the source.** Run `record store-source uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_SOURCE" --agent-id e2e-verification store 'The pack fixture contains a launch checklist.' --team verify --task pack-smoke --type semantic --json`. The result returns its key.
- **Export.** Run `record export uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_SOURCE" --agent-id e2e-verification export --out "$VERIFY_PACK" --json`. The command reports the number of exported memories and the file path.
- **Import.** Run `record import uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_REPLICA" --agent-id e2e-verification import "$VERIFY_PACK" --json`. It reports at least one added memory.
- **Confirm and repeat.** Run `record replica-list uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_REPLICA" --agent-id e2e-verification list --task pack-smoke --status active --json` and assert the expected content and key are present. Run `record import-again uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_REPLICA" --agent-id e2e-verification import "$VERIFY_PACK" --json`; it reports zero added and at least one skipped. Run `record replica-list-after-repeat` with the same `list --task pack-smoke --status active --json` CLI command and assert it still shows one copy.
- **Proof.** Save checks for matching identity, imported key/content, and repeat counts in `$VERIFY_EVIDENCE/assertion.py`, then run `record assertion python3 "$VERIFY_EVIDENCE/assertion.py"`. Preserve the export/import outputs, pack file, and replica list output in `VERIFY_EVIDENCE`.

## Gotchas

- The source and destination must be replicas with the same logical `vault_id`; two unrelated `init` commands create different vault identities.
- V1 import is an insert-only union. It does not update an existing key or propagate deletion state.
- Do not use a real project vault or pack containing private memories for this drive.
- Use server push/pull when verifying resumable status updates and deletions; this file-pack recipe does not prove those behaviors.
