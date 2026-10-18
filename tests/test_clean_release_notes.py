import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release" / "clean_release_notes.py"
SPEC = importlib.util.spec_from_file_location("clean_release_notes", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
notes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notes)

GENERATED = """## What's Changed
* fix: scope the scan by @someone in https://github.com/example/repo/pull/80
* chore: bump by @some-bot[bot] in https://github.com/example/repo/pull/82

## New Contributors
* @someone made their first contribution in https://github.com/example/repo/pull/80

**Full Changelog**: https://github.com/example/repo/compare/v1.0.0...v1.0.1
"""

EXPECTED = """## What's Changed
* fix: scope the scan in https://github.com/example/repo/pull/80
* chore: bump in https://github.com/example/repo/pull/82


**Full Changelog**: https://github.com/example/repo/compare/v1.0.0...v1.0.1
"""


def test_clean_removes_attributions_and_new_contributors() -> None:
    assert notes.clean(GENERATED) == EXPECTED
    assert "@" not in notes.clean(GENERATED)


def test_clean_keeps_titles_that_mention_by_and_unrelated_text() -> None:
    text = "* docs: sort by @-mentions policy in https://github.com/example/repo/pull/1\nPlain text by @nobody.\n"
    assert notes.clean(text) == "* docs: sort by @-mentions policy in https://github.com/example/repo/pull/1\nPlain text by @nobody.\n"


def test_clean_keeps_new_contributors_heading_with_other_entries() -> None:
    text = "## New Contributors\n* first entry kept\n"
    assert notes.clean(text) == text


def test_main_rewrites_file_in_place(tmp_path: Path) -> None:
    path = tmp_path / "release-body.txt"
    path.write_text(GENERATED, encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), str(path)], check=False)
    assert result.returncode == 0
    assert path.read_text(encoding="utf-8") == EXPECTED
    assert subprocess.run([sys.executable, str(SCRIPT)], check=False, capture_output=True).returncode == 2
