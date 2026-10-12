# Ingest Markdown documents

Document ingest seeds a vault from Markdown, Markdown-like, and text files under a directory, splitting Markdown at headings and keeping file content searchable as document memories.

## Sub-features

- `ingest-files` accepts Markdown, `.markdown`, and `.txt` files below a directory.
- `ingest-sections` creates a memory for each non-empty heading section.
- `ingest-repeat` leaves unchanged chunks unchanged when the same directory is ingested again.
- `ingest-search` makes ingested content available through Cairn's read commands.

## How to get to it (user POV)

- Run `cairn ingest <directory> --team <team>` in a terminal.
- Use `--type document` to label the imported sections explicitly.
- Run `cairn list --task <file-stem>` or `cairn retrieve "<question>"` to read the ingested content.

## Driving it with the Cairn CLI

Preconditions:

- Follow the isolated-vault setup in `../SKILL.md`; `VERIFY_VAULT` is a fresh SQLite vault with the hash embedder.
- Create a separate fixture directory under `VERIFY_ROOT/docs`; use a filename without a leading dot.

- **Create a document.** Run `mkdir -p "$VERIFY_ROOT/docs"`, then write `launch.md` there with this exact content:

  ~~~markdown
  ## Schedule
  The launch rehearsal starts at 09:30 UTC.

  ## Owner
  Mira owns the launch checklist.
  ~~~

- **Ingest.** Run `record ingest uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_VAULT" --agent-id e2e-verification ingest "$VERIFY_ROOT/docs" --team verify --type document --json`. The result reports one file, two created chunks, and zero flagged near-duplicates.
- **Read the sections back.** Run `record list uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_VAULT" --agent-id e2e-verification list --task launch --status active --json`. Assert the result contains both literal section headings and their body text.
- **Repeat ingest.** Run `record ingest-again uv run --no-project --with-editable "$CAIRN_REPO" cairn --vault "$VERIFY_VAULT" --agent-id e2e-verification ingest "$VERIFY_ROOT/docs" --team verify --type document --json`. It reports two unchanged chunks and zero new chunks. Run `record list-after-repeat` with the same `list --task launch --status active --json` CLI command; assert it still contains exactly two active memories for task `launch`.
- **Proof.** Save checks for the counts, section contents, and repeat result in `$VERIFY_EVIDENCE/assertion.py`, then run `record assertion python3 "$VERIFY_EVIDENCE/assertion.py"`. Keep both ingest outputs and both list outputs in `VERIFY_EVIDENCE`.

## Gotchas

- Only `.md`, `.markdown`, and `.txt` files are read; empty files and files below hidden directories are skipped.
- Each file's stem becomes its task ID after lowercase and punctuation normalization.
- Ingest splits on H1–H3 headings and stores heading text with each non-empty section.
- An unchanged repeat is idempotent; changing a section creates a new memory and may trigger near-duplicate handling.
