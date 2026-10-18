"""Remove account attributions from GitHub-generated release notes.

Generated notes credit each pull request's author (`by @name`) and list new
contributors. Public release text must not name accounts, so this keeps the
pull request titles, links, and the Full Changelog line and drops the rest.
"""

from __future__ import annotations

import re
import sys

ATTRIBUTION = re.compile(r" by @[A-Za-z0-9-]+(?:\[bot\])?(?= in https://)")
NEW_CONTRIBUTOR = re.compile(r"^\* @[A-Za-z0-9-]+(?:\[bot\])? made their first contribution\b")


def clean(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        if NEW_CONTRIBUTOR.match(line):
            continue
        lines.append(ATTRIBUTION.sub("", line))
    out: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() == "## New Contributors":
            following = next((later for later in lines[index + 1 :] if later.strip()), "")
            if not following.startswith("* "):
                continue
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: clean_release_notes.py NOTES_FILE", file=sys.stderr)
        return 2
    path = sys.argv[1]
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(clean(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
