# Store and find memories

The core memory workflow creates a local vault, stores a fact, and finds the persisted fact through Cairn's exact or semantic read commands.

## Sub-features

- `memory-init` creates an isolated SQLite vault with an explicit agent identity.
- `memory-store` writes a fact with a team and task scope.
- `memory-list` confirms stored content through an exact task lookup.
- `memory-retrieve` searches the active memory set by meaning.
- `memory-correction` replaces a fact by storing a newer version with `--supersedes`.

## How to get to it (user POV)

- Run `cairn init --yes --embed-spec hash` in a project.
- Run `cairn store "<fact>" --team <team> --task <task>` in a terminal.
- Run `cairn retrieve "<question>" --team <team> --task <task>` to search by meaning.
- Run `cairn list --task <task>` for an exact scoped listing, or `cairn get <key>` when a key is known.
- Correct a prior fact with `cairn store "<correction>" --team <team> --task <task> --supersedes <key>`.

## Driving it with the Cairn CLI

Preconditions:

- Follow the isolated-vault setup in `../SKILL.md`; `VERIFY_VAULT` already has a SQLite config and `CAIRN_REPO` points to the checkout.
- Use the baseline `doctor` output to confirm `storage` is `sqlite`, `embedder` is `hash-v2/384d`, and `memories` is `0`.

- **Store a fact.** Run `record store uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_VAULT" --agent-id e2e-verification store 'The launch rehearsal starts at 09:30 UTC.' --team verify --task smoke --type semantic --json`. The result has action `created` and a memory key.
- **Find by meaning.** Run `record retrieve uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_VAULT" --agent-id e2e-verification retrieve 'When is the launch rehearsal?' --team verify --task smoke --top-k 5 --json`. Assert the result contains the stored fact.
- **Confirm exact persistence.** Run `record list uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_VAULT" --agent-id e2e-verification list --task smoke --status active --json`. Assert it contains exactly one active memory with content `The launch rehearsal starts at 09:30 UTC.` and the key returned by `store`.
- **Correct the fact.** Set `MEMORY_KEY` from `store.stdout` with `python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["key"])' "$VERIFY_EVIDENCE/store.stdout"`. Run `record correct uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_VAULT" --agent-id e2e-verification store 'The launch rehearsal starts at 10:00 UTC.' --team verify --task smoke --type semantic --supersedes "$MEMORY_KEY" --json`. The result reports action `superseded` and returns a new version key.
- **Confirm the correction.** Run `record active-after-correction uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_VAULT" --agent-id e2e-verification list --task smoke --status active --json` and `record superseded-version uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_VAULT" --agent-id e2e-verification get "$MEMORY_KEY" --json`. Assert the active list has one item with the corrected content and the old key now has status `superseded`.
- **Check health after the write.** Run `record doctor-after uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_VAULT" --agent-id e2e-verification doctor --json`. It reports `memories: 2`, one active and one superseded.
- **Proof.** Save checks for the returned keys, readback content, and status changes in `$VERIFY_EVIDENCE/assertion.py`, then run `record assertion python3 "$VERIFY_EVIDENCE/assertion.py"`. Preserve the baseline `init` and `doctor`, `store`, `retrieve`, `list`, `correct`, `active-after-correction`, `superseded-version`, and `doctor-after` outputs in `VERIFY_EVIDENCE`.

## Gotchas

- Put global options such as `--vault` and `--agent-id` before the command name.
- Cairn otherwise defaults to `./.cairn`, and storage settings can fall back to `~/.cairn/config.toml`; explicitly configure the temporary vault as SQLite.
- Use `hash` for an offline verification run. It is intentionally coarse for near-duplicate detection; do not use this run to claim fastembed quality.
- Do not run `purge` in a verification drive. It permanently deletes a canonical group.
