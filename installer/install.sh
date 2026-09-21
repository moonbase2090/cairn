#!/usr/bin/env sh
# Install (or upgrade) cairn via uv.
#
#   curl -fsSL https://cairncli.com/install.sh | sh
#   CAIRN_REF=main sh install.sh        # pin a branch, tag, or sha
#
# NOTE: plain `uv tool install cairn` pulls an unrelated same-named package
# from PyPI — always install moonbase2090/cairn from git (the default below).
#
# Machine-level only: puts `cairn`, `cairn-mcp`, `cairn-embedd` on PATH.
# Per-project wiring stays manual: `cairn init --yes && cairn bootstrap`.
set -eu

CAIRN_REF="${CAIRN_REF:-main}"
BIN_DIR="${UV_TOOL_BIN_DIR:-$HOME/.local/bin}"
SPEC="git+https://github.com/moonbase2090/cairn.git@${CAIRN_REF}"

die() { printf 'cairn-install: %s\n' "$*" >&2; exit 1; }

# 1. uv ---------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  printf 'cairn-install: installing uv...\n'
  curl -fsSL https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv install finished but uv is not on PATH ($HOME/.local/bin). Add it and re-run."
fi

# 2. python >= 3.11 (uv provisions its own if the system has none) ----
if uv python find ">=3.11" >/dev/null 2>&1; then
  printf 'cairn-install: python %s\n' "$(uv python find '>=3.11')"
else
  printf 'cairn-install: no python >=3.11 found, provisioning one via uv...\n'
  uv python install
fi

# 3. cairn ------------------------------------------------------------
if uv tool list 2>/dev/null | grep -q '^cairn '; then
  printf 'cairn-install: upgrading cairn to %s...\n' "$SPEC"
  uv tool install --force "$SPEC"
else
  printf 'cairn-install: installing %s...\n' "$SPEC"
  uv tool install "$SPEC"
fi

# 4. verify -----------------------------------------------------------
CAIRN_BIN="$(command -v cairn || true)"
[ -n "$CAIRN_BIN" ] || CAIRN_BIN="$BIN_DIR/cairn"
[ -x "$CAIRN_BIN" ] || die "install finished but no executable cairn found (looked on PATH and $BIN_DIR)."
"$CAIRN_BIN" init --help >/dev/null 2>&1 || die "$CAIRN_BIN does not look like moonbase2090/cairn (no 'init' command). Refusing to continue."
printf 'cairn-install: %s\n' "$("$CAIRN_BIN" --version 2>/dev/null || echo 'cairn installed')"
command -v cairn-mcp >/dev/null 2>&1 || printf 'cairn-install: WARNING cairn-mcp not on PATH (expected at %s).\n' "$BIN_DIR/cairn-mcp"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) printf 'cairn-install: NOTE %s is not on PATH. Add this to your shell rc:\n  export PATH="%s:$PATH"\n' "$BIN_DIR" "$BIN_DIR" ;;
esac

printf 'cairn-install: GUI editors (Cursor, etc.) may not see %s — use the absolute binary path in .mcp.json "command".\n' "$BIN_DIR"
printf 'cairn-install: next, per project: cairn init --yes && cairn bootstrap\n'
