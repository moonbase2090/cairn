"""Build a self-contained cairn archive for the current platform.

The archive carries its own CPython, the cairn package, and wrappers for
cairn, cairn-mcp, and cairn-embedd. macOS signing and Windows Authenticode
run only when the corresponding credentials are set.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ENTRIES = ("cairn", "cairn-mcp", "cairn-embedd")
MODULES = {
    "cairn": "cairn.cli",
    "cairn-mcp": "cairn.mcp_server",
    "cairn-embedd": "cairn.embedd",
}
TARGETS = (
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
    "aarch64-apple-darwin",
    "x86_64-apple-darwin",
    "x86_64-pc-windows-msvc",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def host_target() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux" and machine == "x86_64":
        return "x86_64-unknown-linux-gnu"
    if system == "Linux" and machine in ("aarch64", "arm64"):
        return "aarch64-unknown-linux-gnu"
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return "aarch64-apple-darwin"
    if system == "Darwin" and machine == "x86_64":
        return "x86_64-apple-darwin"
    if system == "Windows" and machine in ("amd64", "x86_64"):
        return "x86_64-pc-windows-msvc"
    raise SystemExit(f"unsupported host: {system} {machine}")


def project_version(repo: Path) -> str:
    with (repo / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return str(data["project"]["version"])


def managed_python() -> Path:
    env = os.environ.copy()
    env["UV_NO_PROJECT"] = "1"
    subprocess.run(["uv", "python", "install", "3.12"], check=True, env=env)
    exe = subprocess.check_output(
        ["uv", "python", "find", "3.12", "--managed-python"],
        env=env, text=True,
    ).strip()
    if not exe:
        raise SystemExit("uv did not find a managed CPython 3.12")
    return Path(exe).resolve()


def distribution_root(exe: Path) -> Path:
    if exe.parent.name == "bin":
        return exe.parent.parent
    return exe.parent


def copied_python(root: Path) -> Path:
    unix = root / "python" / "bin" / "python3"
    if unix.exists() or unix.is_symlink():
        return unix.resolve()
    for candidate in (root / "python").glob("python3*"):
        if candidate.is_file() and candidate.parent.name == "bin":
            return candidate
    windows = root / "python" / "python.exe"
    if windows.is_file():
        return windows
    raise SystemExit(f"no interpreter under {root / 'python'}")


def write_unix_wrapper(path: Path, entry: str) -> None:
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "source=$0\n"
        "while [ -L \"$source\" ]; do\n"
        "  dir=$(CDPATH= cd -- \"$(dirname \"$source\")\" && pwd)\n"
        "  link=$(readlink \"$source\")\n"
        "  case \"$link\" in\n"
        "    /*) source=$link ;;\n"
        "    *) source=$dir/$link ;;\n"
        "  esac\n"
        "done\n"
        "root=$(CDPATH= cd -- \"$(dirname \"$source\")/..\" && pwd)\n"
        "export PYTHONPATH=\"$root/app${PYTHONPATH:+:$PYTHONPATH}\"\n"
        f"exec \"$root/python/bin/python3\" \"$root/app/entries/run_{entry}.py\" \"$@\"\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def write_windows_wrapper(path: Path, entry: str) -> None:
    path.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "set \"ROOT=%~dp0..\"\r\n"
        "set \"PYTHONPATH=%ROOT%\\app\"\r\n"
        f"\"%ROOT%\\python\\python.exe\" \"%ROOT%\\app\\entries\\run_{entry}.py\" %*\r\n"
    )


def write_entries(app: Path) -> None:
    entries = app / "entries"
    entries.mkdir(parents=True)
    for name, module in MODULES.items():
        (entries / f"run_{name}.py").write_text(
            "import sys\n"
            f"from {module} import main\n"
            "raise SystemExit(main())\n"
        )


def sign_tree(root: Path, scripts: Path) -> None:
    if platform.system() == "Darwin":
        subprocess.run(["sh", str(scripts / "sign_macos.sh"), str(root)], check=True)
        return
    if platform.system() == "Windows":
        subprocess.run(
            ["powershell", "-File", str(scripts / "sign_windows.ps1"), "-Root", str(root)],
            check=True,
        )
        return
    (root / "SIGNING.txt").write_text("checksums\n")


def archive(root: Path, dest: Path) -> None:
    files = sorted(path for path in root.rglob("*") if path.is_file() and path.name != "SHA256SUMS")
    lines = [f"{sha256(path)}  {path.relative_to(root).as_posix()}\n" for path in files]
    (root / "SHA256SUMS").write_text("".join(lines))
    if dest.suffix == ".zip":
        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(path for path in root.rglob("*") if path.is_file()):
                bundle.write(path, f"{root.name}/{path.relative_to(root).as_posix()}")
        return
    with tarfile.open(dest, "w:gz") as bundle:
        bundle.add(root, arcname=root.name)


def build(repo: Path, out: Path, target: str) -> Path:
    version = project_version(repo)
    scripts = repo / "scripts" / "release"
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / f"cairn-{version}-{target}"
        root.mkdir()
        dist = distribution_root(managed_python())
        shutil.copytree(dist, root / "python", symlinks=True)
        app = root / "app"
        app.mkdir()
        python = copied_python(root)
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), "--target", str(app), "--link-mode", "copy", str(repo)],
            check=True,
        )
        write_entries(app)
        shutil.rmtree(app / "bin", ignore_errors=True)
        for cache in app.rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
        shutil.rmtree(root / "python" / "share", ignore_errors=True)
        bindir = root / "bin"
        bindir.mkdir()
        windows = target.endswith("windows-msvc")
        for name in ENTRIES:
            if windows:
                write_windows_wrapper(bindir / f"{name}.cmd", name)
            else:
                write_unix_wrapper(bindir / name, name)
        (root / "VERSION").write_text(version + "\n")
        (root / "TARGET").write_text(target + "\n")
        shutil.copy2(repo / "LICENSE", root / "MPL-2.0.txt")
        (root / "INSTALL.txt").write_text(
            f"cairn {version} for {target}.\n"
            "Unix: ./install.sh [--prefix /absolute/path]\n"
            "Windows: powershell -File .\\install.ps1 [-Prefix C:\\absolute\\path]\n"
            "The installer puts cairn, cairn-mcp, and cairn-embedd on the prefix bin path.\n"
            "Then, in each project: cairn init --yes && cairn bootstrap\n"
        )
        if windows:
            shutil.copy2(scripts / "install.ps1", root / "install.ps1")
        else:
            shutil.copy2(scripts / "install.sh", root / "install.sh")
            (root / "install.sh").chmod(0o755)
        sign_tree(root, scripts)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(app)
        probe = subprocess.run(
            [str(python), str(app / "entries" / "run_cairn.py"), "--version"],
            capture_output=True, text=True, env=env, check=False,
        )
        if probe.returncode != 0 or version not in probe.stdout:
            raise SystemExit(
                "packaged cairn failed:\n"
                f"stdout: {probe.stdout}\nstderr: {probe.stderr}"
            )
        suffix = ".zip" if windows else ".tar.gz"
        dest = out / f"cairn-{version}-{target}{suffix}"
        archive(root, dest)
    checksums = out / "SHA256SUMS"
    archives = sorted(path for path in out.iterdir() if path.is_file() and path.name != "SHA256SUMS")
    checksums.write_text("".join(f"{sha256(path)}  {path.name}\n" for path in archives))
    return dest


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=TARGETS)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    target = args.target or host_target()
    if target != host_target():
        raise SystemExit(f"this machine builds {host_target()}, not {target}")
    print(build(repo, args.out.resolve(), target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
