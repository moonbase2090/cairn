#!/usr/bin/env bash
set -euo pipefail

root="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d "/tmp/cairn-agent-skill-proof.XXXXXX")"
trap 'rm -rf -- "$tmp"' EXIT

cairn_bin="$(cd "$root" && uv run python -c 'import shutil; print(shutil.which("cairn"))')"
[[ -n "$cairn_bin" && -x "$cairn_bin" ]] || {
  echo "uv did not provide the Cairn console script" >&2
  exit 1
}

mkdir -p "$tmp/bin" "$tmp/home/.codex" "$tmp/home/.claude"
cat >"$tmp/bin/cairn" <<'SH'
#!/bin/sh
exec "$CAIRN_PROOF_CAIRN" "$@"
SH
chmod +x "$tmp/bin/cairn"

export CAIRN_PROOF_CAIRN="$cairn_bin"
export HOME="$tmp/home"
export PATH="$tmp/bin:/usr/bin:/bin:/usr/sbin:/sbin"
unset CAIRN_EXPERIMENTAL_SKILLS || true

if output="$(cairn skills install --agent detected 2>&1)"; then
  echo "skills install ran without its feature gate" >&2
  exit 1
fi
printf '%s\n' "$output" | grep -F "off by default"

output="$(CAIRN_EXPERIMENTAL_SKILLS=1 cairn skills install --agent detected)"
printf '%s\n' "$output" | grep -F "codex: installed"
printf '%s\n' "$output" | grep -F "claude: installed"
cmp "$root/skills/cairn/SKILL.md" "$HOME/.codex/skills/cairn/SKILL.md"
cmp "$root/skills/cairn/SKILL.md" "$HOME/.claude/skills/cairn/SKILL.md"
[[ ! -e "$HOME/.agents" && ! -e "$HOME/.cursor" && ! -e "$HOME/.kiro" ]]
[[ ! -e "$HOME/.config/muse" && ! -e "$HOME/.muse" ]]

output="$(CAIRN_EXPERIMENTAL_SKILLS=1 cairn skills install --agent detected --check)"
printf '%s\n' "$output" | grep -F "codex: current"
printf '%s\n' "$output" | grep -F "claude: current"

printf 'user edit\n' >"$HOME/.codex/skills/cairn/SKILL.md"
if CAIRN_EXPERIMENTAL_SKILLS=1 cairn skills install --agent detected >"$tmp/conflict" 2>&1; then
  echo "install overwrote an edited skill without --force" >&2
  exit 1
fi
grep -F "use --force" "$tmp/conflict"
grep -F "user edit" "$HOME/.codex/skills/cairn/SKILL.md"

output="$(CAIRN_EXPERIMENTAL_SKILLS=1 cairn skills install --agent detected --force)"
printf '%s\n' "$output" | grep -F "codex: replaced"
cmp "$root/skills/cairn/SKILL.md" "$HOME/.codex/skills/cairn/SKILL.md"

empty_home="$tmp/empty-home"
mkdir -p "$empty_home"
output="$(HOME="$empty_home" CAIRN_EXPERIMENTAL_SKILLS=1 cairn skills install --agent detected)"
printf '%s\n' "$output" | grep -F "no supported agents detected"
[[ ! -e "$empty_home/.codex" && ! -e "$empty_home/.claude" ]]

cat >"$tmp/bin/uv" <<'SH'
#!/bin/sh
case "$1 $2" in
  "python find") printf '/tmp/python\n' ;;
  "tool list") printf 'cairn 0.12.1\n' ;;
  "tool install") exit 0 ;;
  *) exit 1 ;;
esac
SH
chmod +x "$tmp/bin/uv"

hook_home="$tmp/hook-home"
mkdir -p "$hook_home/.codex" "$hook_home/.claude"
output="$(HOME="$hook_home" XDG_CONFIG_HOME="$hook_home/.config" \
  UV_TOOL_BIN_DIR="$tmp/bin" sh "$root/installer/install.sh")"
printf '%s\n' "$output" | grep -F "codex: installed"
printf '%s\n' "$output" | grep -F "claude: installed"
cmp "$root/skills/cairn/SKILL.md" "$hook_home/.codex/skills/cairn/SKILL.md"
cmp "$root/skills/cairn/SKILL.md" "$hook_home/.claude/skills/cairn/SKILL.md"

opt_out_home="$tmp/installer-opt-out"
mkdir -p "$opt_out_home/.codex"
output="$(HOME="$opt_out_home" XDG_CONFIG_HOME="$opt_out_home/.config" \
  UV_TOOL_BIN_DIR="$tmp/bin" CAIRN_NO_AGENT_SKILLS=1 \
  sh "$root/installer/install.sh")"
printf '%s\n' "$output" | grep -F "skipped automatic Agent Skill installation"
[[ ! -e "$opt_out_home/.codex/skills" ]]

printf 'Proof passed: the bundled skill installs only for detected agents, check is read-only, edits are preserved, force replaces the selected skill, and the installer hook honors its opt-out.\n'
