import os
import shutil
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[1] / "githooks" / "pre-commit"


def _git(repo: Path, *args: str, env=None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    return repo


def _hook(repo: Path, *, path_env: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if path_env is not None:
        env["PATH"] = path_env
    return subprocess.run(
        [sys.executable, str(HOOK)],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def test_hook_checks_staged_blob_instead_of_unstaged_working_tree(tmp_path):
    repo = _repo(tmp_path)
    source = repo / "example.py"
    source.write_text("value = 1\n")
    _git(repo, "add", "example.py")
    source.write_text("value=1\n")

    result = _hook(repo)

    assert result.returncode == 0, result.stdout


def test_hook_rejects_unformatted_staged_blob_even_if_worktree_is_formatted(
    tmp_path,
):
    repo = _repo(tmp_path)
    source = repo / "example.py"
    source.write_text("value=1\n")
    _git(repo, "add", "example.py")
    source.write_text("value = 1\n")

    result = _hook(repo)

    assert result.returncode == 1
    assert "Fix formatting with: ruff format" in result.stdout


def test_hook_rejects_staged_python_when_ruff_is_missing(tmp_path):
    repo = _repo(tmp_path)
    (repo / "example.py").write_text("value = 1\n")
    _git(repo, "add", "example.py")
    path = tmp_path / "bin"
    path.mkdir()
    git = shutil.which("git")
    assert git is not None
    (path / "git").symlink_to(git)

    result = _hook(repo, path_env=str(path))

    assert result.returncode == 1
    assert 'Install the dev tools with: pip install -e ".[dev]"' in result.stdout


def test_hook_allows_non_python_staged_changes_without_ruff(tmp_path):
    repo = _repo(tmp_path)
    (repo / "notes.md").write_text("no Python formatting check needed\n")
    _git(repo, "add", "notes.md")
    path = tmp_path / "bin"
    path.mkdir()
    git = shutil.which("git")
    assert git is not None
    (path / "git").symlink_to(git)

    result = _hook(repo, path_env=str(path))

    assert result.returncode == 0, result.stdout
