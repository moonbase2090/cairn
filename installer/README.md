# Cairn installers

Two ways onto a new machine. Both are machine-level only (tool + `PATH`);
per-project wiring always stays manual: `cairn init --yes && cairn bootstrap`.

## Release archives

GitHub Actions builds three archives when a `vX.Y.Z` tag is pushed:
`x86_64-unknown-linux-gnu`, `aarch64-apple-darwin`, and
`x86_64-pc-windows-msvc`. Each archive contains its own Python, `cairn`,
`cairn-mcp`, and `cairn-embedd`. Unix archives install with `./install.sh`.
The Windows archive installs with `powershell -File .\install.ps1`.

macOS Developer ID signing and notarization run when `CAIRN_CODESIGN_IDENTITY`,
`APPLE_ID`, `APPLE_TEAM_ID`, and `APPLE_APP_PASSWORD` are repository secrets.
Windows Authenticode signing runs when `WINDOWS_CERT_BASE64` and
`WINDOWS_CERT_PASSWORD` are set. Without those secrets the archive is built
and checksummed, and `SIGNING.txt` says `unsigned`.

## Shell script (Linux + macOS)

```sh
curl -fsSL https://cairncli.com/install.sh | sh
CAIRN_REF=main sh install.sh   # pin a branch, tag, or sha
```

What it does: installs `uv` if missing (official installer), ensures a
Python `>=3.11` (`uv python install` fallback), installs/upgrades cairn
from `git+https://github.com/moonbase2090/cairn.git`, guards that the
resulting binary actually has the `init` command, and warns when the tool
bin dir is off `PATH` (including the GUI-editor caveat for `.mcp.json`).

## Homebrew tap (macOS)

```sh
brew tap moonbase2090/tap
brew install cairn
```

Formula lives in `homebrew/cairn.rb`. It stages the `uv tool` install
inside the Cellar (`UV_TOOL_DIR`/`UV_TOOL_BIN_DIR` into prefix) so
`brew link` puts the binaries on `PATH`. Requires network at install time
(acceptable for a personal tap). Untagged HEAD install for now — switch to
a versioned tarball + `sha256` once releases/tags exist.

Neither `brew` nor macOS is available on the dev machine, so the formula
is written but not brew-tested. Test on a Mac with:

```sh
brew install --build-from-source ./homebrew/cairn.rb
brew test cairn
```

## ⚠️ PyPI name squat (open issue)

The name `cairn` on PyPI (0.2.3) is an unrelated package with a different
CLI (`update/up/create/new/release`, single binary, no `cairn-mcp`).
Consequences:

- `uv tool install cairn` installs the WRONG tool — including the command
  currently shown on cairncli.com. The site must say
  `uv tool install git+https://github.com/moonbase2090/cairn.git`
  until this is resolved.
- The installer deliberately has no PyPI path for exactly this reason.

Fix options: publish under a scoped/different name (`cairn-cli`,
`moonbase-cairn`), claim/replace the squatting release, or keep git-only
installs permanently.
