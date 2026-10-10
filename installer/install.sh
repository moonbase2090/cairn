#!/usr/bin/env sh
# Install (or upgrade) cairn via uv.
#
#   curl -fsSL https://cairncli.com/install.sh | sh
#   CAIRN_REF=main sh install.sh        # pin a branch, tag, or sha
#   CAIRN_INSTALL_EMBED=fastembed sh install.sh  # include fastembed support
#   CAIRN_INSTALL_AWS=1 sh install.sh           # include the AWS SDK
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
INSTALL_EMBED="${CAIRN_INSTALL_EMBED:-none}"
INSTALL_AWS="${CAIRN_INSTALL_AWS:-0}"

die() { printf 'cairn-install: %s\n' "$*" >&2; exit 1; }

case "$INSTALL_EMBED" in
  none|fastembed) ;;
  *) die "CAIRN_INSTALL_EMBED must be 'none' or 'fastembed'." ;;
esac
case "$INSTALL_AWS" in
  0|1) ;;
  *) die "CAIRN_INSTALL_AWS must be 0 or 1." ;;
esac

if [ "${CAIRN_NO_AGENT_SKILLS:-0}" = "1" ]; then
  SKIP_AGENT_SKILLS=1
else
  SKIP_AGENT_SKILLS=0
fi

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
set --
if [ "$INSTALL_EMBED" = "fastembed" ]; then
  set -- "$@" --with 'fastembed>=0.3'
fi
if [ "$INSTALL_AWS" = "1" ]; then
  set -- "$@" --with 'boto3>=1.40' --with 'botocore>=1.40'
fi

if uv tool list 2>/dev/null | grep -q '^cairn '; then
  printf 'cairn-install: upgrading cairn to %s...\n' "$SPEC"
  uv tool install --force "$@" "$SPEC"
else
  printf 'cairn-install: installing %s...\n' "$SPEC"
  uv tool install "$@" "$SPEC"
fi

# 4. verify -----------------------------------------------------------
CAIRN_BIN="$(command -v cairn || true)"
[ -n "$CAIRN_BIN" ] || CAIRN_BIN="$BIN_DIR/cairn"
[ -x "$CAIRN_BIN" ] || die "install finished but no executable cairn found (looked on PATH and $BIN_DIR)."
"$CAIRN_BIN" init --help >/dev/null 2>&1 || die "$CAIRN_BIN does not look like moonbase2090/cairn (no 'init' command). Refusing to continue."
printf 'cairn-install: %s\n' "$("$CAIRN_BIN" --version 2>/dev/null || echo 'cairn installed')"
command -v cairn-mcp >/dev/null 2>&1 || printf 'cairn-install: WARNING cairn-mcp not on PATH (expected at %s).\n' "$BIN_DIR/cairn-mcp"

if [ "$SKIP_AGENT_SKILLS" = "1" ]; then
  printf 'cairn-install: skipped automatic Agent Skill installation; run CAIRN_EXPERIMENTAL_SKILLS=1 %s skills install --agent detected to install it later.\n' "$CAIRN_BIN"
elif ! CAIRN_EXPERIMENTAL_SKILLS=1 "$CAIRN_BIN" skills install --agent detected; then
  printf 'cairn-install: WARNING Cairn installed, but automatic Agent Skill installation failed. Retry with CAIRN_EXPERIMENTAL_SKILLS=1 %s skills install --agent detected, or skip it with CAIRN_NO_AGENT_SKILLS=1.\n' "$CAIRN_BIN" >&2
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) printf 'cairn-install: NOTE %s is not on PATH. Add this to your shell rc:\n  export PATH="%s:$PATH"\n' "$BIN_DIR" "$BIN_DIR" ;;
esac

printf 'cairn-install: GUI editors (Cursor, etc.) may not see %s — use the absolute binary path in .mcp.json "command".\n' "$BIN_DIR"
if [ "$INSTALL_EMBED" = "fastembed" ] || [ "$INSTALL_AWS" = "1" ]; then
  printf 'cairn-install: optional packages installed:'
  [ "$INSTALL_EMBED" != "fastembed" ] || printf ' fastembed'
  [ "$INSTALL_AWS" != "1" ] || printf ' AWS SDK (boto3, botocore)'
  printf '\n'
  printf 'cairn-install: repeat the same CAIRN_INSTALL_* flags when upgrading to retain these optional packages.\n'
fi
if [ "$INSTALL_AWS" = "1" ]; then
  printf 'cairn-install: AWS deployment also needs the AWS CLI, Node.js, and npm; Cairn installs its pinned CDK npm dependencies on demand.\n'
fi
printf 'cairn-install: next, per project: cairn init --yes && cairn bootstrap\n'
