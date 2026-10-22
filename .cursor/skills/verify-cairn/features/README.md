# Cairn verification map

This map covers Cairn's main local-first CLI workflows. Read the index before
driving a command, then follow the matching feature recipe.

## Baseline preconditions

- Run from the Cairn repository root and set `CAIRN_REPO="$PWD"`.
- Use a fresh `VERIFY_ROOT` and keep proof in a separate `VERIFY_EVIDENCE` directory.
- Create `VERIFY_VAULT/config.toml` with `[storage] backend = "sqlite"` before `init` so a personal config cannot select another backend.
- Pass `--vault "$VERIFY_VAULT"`, use `--embed-spec hash`, and set an `e2e-*` agent ID on every initialization.
- Run the health check from `../SKILL.md` before each fresh feature drive and again after a failed command.
- Use `record` from `../SKILL.md` for commands whose output is part of the proof.

## Driving conventions

- Each Cairn CLI command is a new, short-lived process. Use one disposable vault per feature drive.
- Start from the fixture described by the feature file. Do not use the repository's `.cairn` directory or `~/.cairn` as test data.
- Verify mutations with a second CLI read and an assertion over its literal output.
- Run project bootstrap only from a temporary project directory; it writes `.mcp.json` and `AGENTS.md` in the current working directory.
- Keep service and cloud integrations outside this local map unless the PR supplies a safe local mock or isolated test endpoint.

## Proof and skip reporting

- Capture the user command, stdout, stderr, and exit code.
- For writes, capture a second read that proves the expected content and state persisted.
- If a feature needs an unavailable configured server or entitlement, name the exact attempted route and unmet prerequisite; do not call a different route equivalent proof.
- Evidence must survive cleanup. Record the feature ID and user entry point with the evidence summary.

## Features

- [Store and find memories](./memory-workflow.md) covers the core local memory path.
- [Ingest Markdown documents](./document-ingest.md) covers file seeding and repeat-ingest behavior.
- [Bootstrap a project](./project-bootstrap.md) covers the project files and onboarding pack written by Cairn.
- [Share a portable pack](./portable-packs.md) covers export and import through files.
