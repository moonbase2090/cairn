"""Enforce conventional-commit PR titles and single-step semver bumps.

Rules:
  - PR title must be `<type>[!]: <desc>` or `<type>(scope)[!]: <desc>`.
  - `!`                       -> requires a MAJOR bump
  - `feat`                    -> requires a MINOR bump
  - anything else             -> requires a PATCH bump
  - If `src/**` or `pyproject.toml` changed, `pyproject.toml` version MUST
    increase by exactly the required level versus the PR base.
  - If only other files changed, the version MAY stay the same; if it
    moves it must still be a valid increase.
"""
import os
import re
import subprocess
import sys

TITLE_RE = re.compile(
    r"^(feat|fix|docs|chore|refactor|test|build|ci|perf|revert)"
    r"(\([^)]+\))?(!)?: .+"
)
VERSION_RE = re.compile(r'^version\s*=\s*"(\d+\.\d+\.\d+)"', re.M)


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def version_at(ref):
    """Return the pyproject version at a git ref, or None."""
    try:
        if ref == "WORKTREE":
            text = open("pyproject.toml").read()
        else:
            text = sh("git", "show", f"{ref}:pyproject.toml")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    m = VERSION_RE.search(text)
    return m.group(1) if m else None


def bump_level(old, new):
    """Highest semver component that changed: major/minor/patch/None."""
    o = tuple(map(int, old.split(".")))
    n = tuple(map(int, new.split(".")))
    if n <= o:
        return None
    for name, a, b in zip(("major", "minor", "patch"), o, n):
        if b != a:
            return name
    return None


def main():
    base = os.environ["BASE_SHA"]
    title = os.environ["PR_TITLE"]

    m = TITLE_RE.match(title)
    if not m:
        print(f"FAIL: PR title is not a conventional commit: {title!r}")
        print("      expected `<type>[!]: <desc>, e.g. `feat: add galaxy zoom`")
        return 1
    required = "major" if m.group(3) else ("minor" if m.group(1) == "feat" else "patch")
    print(f"title OK: {title!r} -> requires {required} bump")

    old = version_at(base)
    new = version_at("WORKTREE")
    if old is None or new is None:
        print(f"FAIL: could not read version (base={old}, head={new})")
        return 1

    changed = sh("git", "diff", "--name-only", f"{base}...HEAD").split()
    src_touched = any(p == "pyproject.toml" or p.startswith("src/") for p in changed)
    print(f"base={old} head={new} src_touched={src_touched}")

    level = bump_level(old, new)
    if src_touched:
        if level is None:
            print(f"FAIL: src/ changed but version did not increase ({old} -> {new})")
            return 1
        if level != required:
            print(f"FAIL: title requires {required} bump but version went {old} -> {new} ({level})")
            return 1
    elif level is not None and not (tuple(map(int, new.split("."))) > tuple(map(int, old.split(".")))):
        print(f"FAIL: invalid version change {old} -> {new}")
        return 1

    print(f"PASS: {old} -> {new}" + (f" ({level})" if level else " (unchanged, src untouched)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
