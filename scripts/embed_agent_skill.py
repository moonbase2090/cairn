"""Copy the source Agent Skill into Cairn's installable package."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path


def paths() -> tuple[Path, Path]:
    repo = Path(__file__).resolve().parents[1]
    return repo / "skills" / "cairn" / "SKILL.md", repo / "src" / "cairn" / "agent_skill.md"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the packaged copy differs from the source skill.",
    )
    args = parser.parse_args()
    source, destination = paths()
    content = source.read_bytes()
    if args.check:
        if not destination.is_file() or destination.read_bytes() != content:
            print(
                "The packaged Agent Skill differs from skills/cairn/SKILL.md. "
                "Run python scripts/embed_agent_skill.py.",
                file=sys.stderr,
            )
            return 1
        print("The packaged Agent Skill matches skills/cairn/SKILL.md.")
        return 0

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".agent_skill.", suffix=".tmp",
            dir=destination.parent, delete=False,
        ) as temporary:
            temp_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temp_path, destination)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    print(f"Updated {destination.relative_to(source.parents[2])}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
