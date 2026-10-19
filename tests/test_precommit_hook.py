from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SOURCE = REPO_ROOT / "githooks" / "pre-commit"
INSTALLER_SOURCE = REPO_ROOT / "scripts" / "install-hooks.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo_with_hook(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    hooks = repo / "githooks"
    hooks.mkdir()
    shutil.copy2(HOOK_SOURCE, hooks / "pre-commit")
    scripts = repo / "scripts"
    scripts.mkdir()
    shutil.copy2(INSTALLER_SOURCE, scripts / "install-hooks.sh")
    shutil.copy2(REPO_ROOT / "pyproject.toml", repo / "pyproject.toml")
    subprocess.run([str(scripts / "install-hooks.sh")], cwd=repo, check=True)
    return repo


def _run_hook(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "hook", "run", "pre-commit"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


def test_hook_rejects_unformatted_staged_blob_with_spaces_when_worktree_is_formatted(
    tmp_path: Path,
) -> None:
    repo = _repo_with_hook(tmp_path)
    path = Path("package with spaces") / "staged file.py"
    target = repo / path
    target.parent.mkdir()
    target.write_text("value=1\n")
    _git(repo, "add", "--", path.as_posix())

    target.write_text("value = 1\n")
    result = _run_hook(repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Fix formatting with: ruff format" in result.stdout + result.stderr


def test_hook_accepts_formatted_staged_blob_when_worktree_is_unformatted(
    tmp_path: Path,
) -> None:
    repo = _repo_with_hook(tmp_path)
    path = Path("staged.py")
    target = repo / path
    target.write_text("value = 1\n")
    _git(repo, "add", "--", path.as_posix())

    target.write_text("value=1\n")
    result = _run_hook(repo)

    assert result.returncode == 0, result.stdout + result.stderr


def test_hook_accepts_staged_non_python_files(tmp_path: Path) -> None:
    repo = _repo_with_hook(tmp_path)
    (repo / "README.md").write_text("documentation\n")
    _git(repo, "add", "README.md")

    result = _run_hook(repo)

    assert result.returncode == 0, result.stdout + result.stderr
