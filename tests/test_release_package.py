from __future__ import annotations

import importlib.util
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

PACKAGE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "release" / "package.py"
)
PACKAGE_SPEC = importlib.util.spec_from_file_location("release_package", PACKAGE_PATH)
assert PACKAGE_SPEC is not None and PACKAGE_SPEC.loader is not None
package = importlib.util.module_from_spec(PACKAGE_SPEC)
PACKAGE_SPEC.loader.exec_module(package)


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Linux", "x86_64", "x86_64-unknown-linux-gnu"),
        ("Linux", "aarch64", "aarch64-unknown-linux-gnu"),
        ("Linux", "arm64", "aarch64-unknown-linux-gnu"),
        ("Darwin", "arm64", "aarch64-apple-darwin"),
        ("Darwin", "aarch64", "aarch64-apple-darwin"),
        ("Darwin", "x86_64", "x86_64-apple-darwin"),
        ("Windows", "amd64", "x86_64-pc-windows-msvc"),
        ("Windows", "x86_64", "x86_64-pc-windows-msvc"),
    ],
)
def test_host_target_maps_supported_platforms(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    expected: str,
) -> None:
    monkeypatch.setattr(package.platform, "system", lambda: system)
    monkeypatch.setattr(package.platform, "machine", lambda: machine)

    assert package.host_target() == expected


def test_host_target_rejects_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(package.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(package.platform, "machine", lambda: "riscv64")

    with pytest.raises(SystemExit, match="unsupported host: Plan9 riscv64"):
        package.host_target()


def test_copied_python_resolves_unix_symlink(tmp_path: Path) -> None:
    root = tmp_path / "distribution"
    binary_dir = root / "python" / "bin"
    binary_dir.mkdir(parents=True)
    versioned = binary_dir / "python3.12"
    versioned.write_text("interpreter")
    (binary_dir / "python3").symlink_to(versioned.name)

    assert package.copied_python(root) == versioned.resolve()


def test_copied_python_finds_versioned_unix_interpreter(tmp_path: Path) -> None:
    root = tmp_path / "distribution"
    binary_dir = root / "python" / "bin"
    binary_dir.mkdir(parents=True)
    interpreter = binary_dir / "python3.12"
    interpreter.write_text("interpreter")

    assert package.copied_python(root) == interpreter


def test_copied_python_finds_windows_interpreter(tmp_path: Path) -> None:
    root = tmp_path / "distribution"
    python_dir = root / "python"
    python_dir.mkdir(parents=True)
    interpreter = python_dir / "python.exe"
    interpreter.write_text("interpreter")

    assert package.copied_python(root) == interpreter


def test_copied_python_reports_missing_interpreter(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no interpreter under"):
        package.copied_python(tmp_path / "distribution")


@pytest.mark.parametrize("suffix", [".zip", ".tar.gz"])
def test_archive_writes_checksum_manifest_and_payload(
    tmp_path: Path, suffix: str
) -> None:
    root = tmp_path / "payload"
    (root / "bin").mkdir(parents=True)
    (root / "VERSION").write_text("test-version\n")
    (root / "bin" / "cairn").write_text("launcher\n")
    (root / "SHA256SUMS").write_text("stale checksum\n")
    dest = tmp_path / f"package{suffix}"

    package.archive(root, dest)

    manifest = (root / "SHA256SUMS").read_text()
    assert f"{package.sha256(root / 'VERSION')}  VERSION" in manifest
    assert "SHA256SUMS" not in manifest
    if suffix == ".zip":
        with zipfile.ZipFile(dest) as bundle:
            names = set(bundle.namelist())
            assert "payload/VERSION" in names
            assert "payload/SHA256SUMS" in names
            assert bundle.read("payload/bin/cairn") == b"launcher\n"
    else:
        with tarfile.open(dest, "r:gz") as bundle:
            names = set(bundle.getnames())
            assert "payload/VERSION" in names
            assert "payload/SHA256SUMS" in names
            launcher = bundle.extractfile("payload/bin/cairn")
            assert launcher is not None and launcher.read() == b"launcher\n"


def _build_fixture(tmp_path: Path, target: str) -> tuple[Path, Path]:
    repo = tmp_path / "checkout"
    release = repo / "scripts" / "release"
    release.mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "cairn"\nversion = "test-version"\n'
    )
    (repo / "LICENSE").write_text("license\n")
    (release / "install.sh").write_text("installer\n")
    (release / "install.ps1").write_text("installer\n")

    distribution = tmp_path / "managed-python"
    if target.endswith("windows-msvc"):
        interpreter = distribution / "python.exe"
    else:
        interpreter = distribution / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("interpreter")
    (distribution / "share").mkdir()
    (distribution / "share" / "runtime.txt").write_text("runtime\n")
    return repo, interpreter


@pytest.mark.parametrize(
    ("target", "suffix", "launcher"),
    [
        ("x86_64-unknown-linux-gnu", ".tar.gz", "cairn"),
        ("x86_64-pc-windows-msvc", ".zip", "cairn.cmd"),
    ],
)
def test_build_produces_clean_platform_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    suffix: str,
    launcher: str,
) -> None:
    repo, interpreter = _build_fixture(tmp_path, target)
    out = tmp_path / "dist"
    out.mkdir()
    stale_tar = out / "cairn-old.tar.gz"
    stale_zip = out / "cairn-old.zip"
    stale_checksums = out / "SHA256SUMS"
    stale_tar.write_text("stale")
    stale_zip.write_text("stale")
    stale_checksums.write_text("stale")
    monkeypatch.setattr(package, "managed_python", lambda: interpreter)
    monkeypatch.setattr(package, "sign_tree", lambda _root, _scripts: None)
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[:3] == ["uv", "pip", "install"]:
            app = Path(command[command.index("--target") + 1])
            dist_info = app / "cairn-test-version.dist-info"
            dist_info.mkdir()
            (dist_info / "direct_url.json").write_text("checkout metadata")
            (app / "bin").mkdir()
            cache = app / "cairn" / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "stale.pyc").write_bytes(b"cache")
            return subprocess.CompletedProcess(command, 0)
        return subprocess.CompletedProcess(
            command, 0, stdout="cairn test-version\n", stderr=""
        )

    monkeypatch.setattr(package, "subprocess", SimpleNamespace(run=run))

    dest = package.build(repo, out, target)

    assert dest.name == f"cairn-test-version-{target}{suffix}"
    assert dest.exists()
    assert not stale_tar.exists() and not stale_zip.exists()
    assert stale_checksums.read_text() != "stale"
    assert len(commands) == 2
    checksum = (out / "SHA256SUMS").read_text()
    assert f"{package.sha256(dest)}  {dest.name}" in checksum

    root_name = f"cairn-test-version-{target}"
    if suffix == ".zip":
        with zipfile.ZipFile(dest) as bundle:
            names = set(bundle.namelist())
            assert f"{root_name}/bin/{launcher}" in names
            assert f"{root_name}/app/entries/run_cairn.py" in names
            assert f"{root_name}/install.ps1" in names
            assert not any("direct_url.json" in name for name in names)
            assert not any("__pycache__" in name for name in names)
            assert not any("/app/bin/" in name for name in names)
            assert not any("/python/share/" in name for name in names)
    else:
        with tarfile.open(dest, "r:gz") as bundle:
            names = set(bundle.getnames())
            assert f"{root_name}/bin/{launcher}" in names
            assert f"{root_name}/app/entries/run_cairn.py" in names
            assert f"{root_name}/install.sh" in names
            assert not any("direct_url.json" in name for name in names)
            assert not any("__pycache__" in name for name in names)
            assert not any("/app/bin/" in name for name in names)
            assert not any("/python/share/" in name for name in names)


def test_build_rejects_failed_runtime_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = "x86_64-unknown-linux-gnu"
    repo, interpreter = _build_fixture(tmp_path, target)
    out = tmp_path / "dist"
    monkeypatch.setattr(package, "managed_python", lambda: interpreter)
    monkeypatch.setattr(package, "sign_tree", lambda _root, _scripts: None)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["uv", "pip", "install"]:
            return subprocess.CompletedProcess(command, 0)
        return subprocess.CompletedProcess(
            command, 1, stdout="probe output", stderr="probe error"
        )

    monkeypatch.setattr(package, "subprocess", SimpleNamespace(run=run))

    with pytest.raises(SystemExit, match="packaged cairn failed:"):
        package.build(repo, out, target)

    assert not list(out.glob("cairn-*.tar.gz"))
