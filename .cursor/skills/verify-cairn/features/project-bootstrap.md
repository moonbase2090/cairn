# Bootstrap a project

Project bootstrap connects a Cairn vault to an agent host by writing an MCP server entry, adding Cairn instructions to `AGENTS.md`, and seeding the onboarding memories.

## Sub-features

- `bootstrap-mcp` adds the named Cairn server entry to `.mcp.json`.
- `bootstrap-agents` writes or refreshes Cairn's marked section in `AGENTS.md`.
- `bootstrap-pack` seeds the onboarding memories unless `--no-seed` is supplied.
- `bootstrap-name` keeps a named MCP entry pinned to the vault it was created for.

## How to get to it (user POV)

- Run `cairn bootstrap` from the project directory after initializing a vault.
- Run `cairn bootstrap --name cairn-personal` to register an additional vault under a separate MCP entry name.
- Trust the project folder and reload its MCP servers in the agent host before using the new connection.

## Driving it with the Cairn CLI

Preconditions:

- Set `VERIFY_PROJECT="$VERIFY_ROOT/project"` and use this disposable project directory; bootstrap writes `.mcp.json` and `AGENTS.md` in the current directory.
- Create a separate temporary SQLite vault with the baseline in `../SKILL.md`, then change the current directory to the disposable project before running bootstrap.
- Keep `CAIRN_REPO` and `VERIFY_EVIDENCE` as absolute paths so the editable package and evidence remain reachable after changing directories.

- **Prepare the project.** Run `mkdir -p "$VERIFY_PROJECT"`, then change the current directory to `VERIFY_PROJECT`. The baseline `init` and `doctor` already prepared and checked the empty SQLite vault.
- **Bootstrap.** From `VERIFY_ROOT/project`, run `record bootstrap uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_VAULT" bootstrap --name cairn-verification --agent-id e2e-verification --json`. The result names the MCP entry and the two files written.
- **Inspect the files.** Copy the temporary project's `.mcp.json` and `AGENTS.md` to `VERIFY_EVIDENCE` before cleanup. Parse `.mcp.json`; assert `mcpServers.cairn-verification.command` is `cairn-mcp` and its `CAIRN_DIR` resolves to `VERIFY_VAULT`. Assert `AGENTS.md` contains Cairn's marked instructions. Run `record onboarding-list uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_VAULT" --agent-id e2e-verification list --task cairn-onboarding --status active --json`; the seeded onboarding memories appear there.
- **Proof.** Save checks for the MCP entry, generated instructions, and onboarding records in `$VERIFY_EVIDENCE/assertion.py`, then run `record assertion python3 "$VERIFY_EVIDENCE/assertion.py"`. Preserve the bootstrap JSON, both generated project files, and onboarding list output in `VERIFY_EVIDENCE`; they are temporary verification artifacts, not project configuration.

## Gotchas

- Bootstrap writes into the current directory. Never drive it from the Cairn checkout or a real user project.
- Use a new `--name` when registering a second vault. Cairn refuses to reuse an entry name that already points at a different vault.
- The local files do not by themselves activate the host integration. The host must trust the folder and reload its MCP servers.
- `--no-seed` skips onboarding memories; omit it when verifying the onboarding pack.
