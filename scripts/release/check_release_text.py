#!/usr/bin/env python3
"""Check public release inputs for generic and locally configured deny patterns."""

from __future__ import annotations

import bz2
import gzip
import io
import ipaddress
import lzma
import os
import re
import struct
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ALLOWED_EMAIL = "322824348+mb2090@users.noreply.github.com"
PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)
IPV4_CANDIDATE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
IPV6_CANDIDATE = re.compile(
    r"(?<![0-9a-f:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?![0-9a-f:])",
    re.IGNORECASE,
)
URL_USERINFO_PREFIX = re.compile(r"://[^\s/@:]*:$")
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
ASCII_STRINGS = re.compile(rb"[\x20-\x7e\t]{4,}")
UTF16_STRINGS = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
TAR_SIGNATURE = b"ustar"
COMPRESSED_TAR_SIGNATURES = (b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00")
ZIP_LOCAL_HEADER = struct.Struct("<IHHHHHIIIHH")
ZIP_LOCAL_SIGNATURE = 0x04034B50
# Top-level directory of a packaged release archive, e.g. cairn-0.15.4-x86_64-unknown-linux-gnu.
RELEASE_ROOT = re.compile(r"^cairn-\d+\.\d+\.\d+(?:-[^/]+)?$")
# Must match TARGETS in scripts/release/package.py.
RELEASE_TARGETS = (
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
    "aarch64-apple-darwin",
    "x86_64-apple-darwin",
    "x86_64-pc-windows-msvc",
)
OWNED_APP_ENTRIES = frozenset({"cairn", "entries", ".lock"})
OWNED_DIST_INFO = re.compile(r"^cairn-[^/]+\.dist-info$")
GENERIC_PATTERNS = (
    re.compile(r"DESKTOP-[A-Z0-9]+", re.IGNORECASE),
    re.compile(r"\b[a-z0-9][a-z0-9-]*\.local\b", re.IGNORECASE),
    re.compile(r"/Users/", re.IGNORECASE),
    re.compile(r"/home/[a-z]", re.IGNORECASE),
    re.compile(r"C:\\+Users", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----", re.IGNORECASE),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
)


class ScanError(Exception):
    """An artifact could not be read completely."""


def configured_patterns() -> list[re.Pattern[str]]:
    patterns = list(GENERIC_PATTERNS)
    sources: list[tuple[str, str]] = []
    denylist = Path.home() / ".config" / "moonbase" / "release-denylist.txt"
    if denylist.is_file():
        try:
            sources.append(("local", denylist.read_text(encoding="utf-8")))
        except OSError:
            print("cannot read local release denylist", file=sys.stderr)
            raise SystemExit(2)
    secret = os.environ.get("RELEASE_DENYLIST", "")
    if secret:
        sources.append(("secret", secret))

    for source, contents in sources:
        for number, line in enumerate(contents.splitlines(), start=1):
            expression = line.strip()
            if not expression:
                continue
            try:
                patterns.append(re.compile(expression))
            except re.error:
                print(
                    f"invalid {source} release denylist pattern on line {number}",
                    file=sys.stderr,
                )
                raise SystemExit(2)
    return patterns


def is_private_ip(text: str) -> bool:
    for candidate in (IPV4_CANDIDATE, IPV6_CANDIDATE):
        for match in candidate.finditer(text):
            try:
                address = ipaddress.ip_address(match.group(0))
            except ValueError:
                continue
            if any(
                address in network
                for network in PRIVATE_NETWORKS
                if network.version == address.version
            ):
                return True
    return False


def line_matches(text: str, patterns: list[re.Pattern[str]]) -> bool:
    if any(pattern.search(text) for pattern in patterns):
        return True
    if is_private_ip(text):
        return True
    return any(
        match.group(0).casefold() != ALLOWED_EMAIL.casefold()
        and not URL_USERINFO_PREFIX.search(text, 0, match.start())
        for match in EMAIL.finditer(text)
    )


def display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.name


def source_files(root: Path) -> set[Path]:
    files: set[Path] = set()
    changelog = root / "CHANGELOG.md"
    if changelog.is_file():
        files.add(changelog)
    files.update(
        path
        for path in root.iterdir()
        if path.is_file() and path.name.casefold().startswith("readme")
    )
    docs = root / "docs"
    if docs.is_dir():
        files.update(path for path in docs.rglob("*") if path.is_file())
    for path in root.iterdir():
        name = path.name.casefold().replace("_", "-")
        if path.is_file() and "release" in name and "note" in name:
            files.add(path)
        if path.is_dir() and name == "release-notes":
            files.update(child for child in path.rglob("*") if child.is_file())
        if path.is_file() and name.startswith("changelog"):
            files.add(path)
    return files


def add_input(path: Path, root: Path, files: set[Path]) -> None:
    if path.is_dir():
        files.update(child for child in path.rglob("*") if child.is_file())
    elif path.is_file():
        files.add(path)
    else:
        print(f"cannot read {display_path(path, root)}:1", file=sys.stderr)
        raise SystemExit(2)


def scan_text(
    text: str,
    display: str,
    patterns: list[re.Pattern[str]],
    findings: set[str],
) -> None:
    for number, line in enumerate(text.splitlines(), start=1):
        if line_matches(line, patterns):
            findings.add(f"{display}:{number}")


def scan_binary(
    data: bytes,
    display: str,
    patterns: list[re.Pattern[str]],
    findings: set[str],
) -> None:
    patterns_and_encodings = ((ASCII_STRINGS, "ascii"), (UTF16_STRINGS, "utf-16le"))
    for string_pattern, encoding in patterns_and_encodings:
        for match in string_pattern.finditer(data):
            text = match.group(0).decode(encoding, errors="ignore")
            if line_matches(text, patterns):
                line = data.count(b"\n", 0, match.start()) + 1
                findings.add(f"{display}:{line}")


def safe_member_label(display: str, index: int) -> str:
    return f"{display}!member-{index}"


def zip_local_extra(data: bytes, member: zipfile.ZipInfo, central_start: int) -> bytes:
    offset = member.header_offset
    if offset < 0 or offset + ZIP_LOCAL_HEADER.size > central_start:
        raise ScanError
    try:
        fields = ZIP_LOCAL_HEADER.unpack_from(data, offset)
    except struct.error as error:
        raise ScanError from error
    if fields[0] != ZIP_LOCAL_SIGNATURE:
        raise ScanError
    filename_size, extra_size = fields[-2:]
    extra_start = offset + ZIP_LOCAL_HEADER.size + filename_size
    extra_end = extra_start + extra_size
    if extra_end > central_start:
        raise ScanError
    return data[extra_start:extra_end]


def project_version(root: Path) -> str | None:
    try:
        with (root / "pyproject.toml").open("rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return None


def is_tracked(path: Path, root: Path) -> bool:
    """True when git tracks the path, or when that cannot be ruled out."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--error-unmatch",
                "--",
                str(path.resolve()),
            ],
            capture_output=True,
            check=False,
        )
    except OSError:
        return True
    return result.returncode != 1


def release_root_for(path: Path, root: Path) -> str | None:
    """Return the archive root for a packaged release artifact in `<repo>/dist`.

    Only an untracked archive in the repository's dist directory named
    `cairn-<version>-<target>.tar.gz` (or `.zip` for Windows), where the
    version is the project version and the target is a packaged release
    target, is trusted to carry bundled third-party files. Every other input
    is scanned in full.
    """
    try:
        if path.resolve().parent != (root / "dist").resolve():
            return None
    except OSError:
        return None
    version = project_version(root)
    if version is None:
        return None
    for target in RELEASE_TARGETS:
        suffix = ".zip" if target.endswith("windows-msvc") else ".tar.gz"
        stem = f"cairn-{version}-{target}"
        if path.name == stem + suffix:
            return None if is_tracked(path, root) else stem
    return None


def is_vendored_member(name: str, release_root: str | None) -> bool:
    """Return True for bundled third-party content in a packaged release archive.

    The bundled Python runtime (`python/`) and every `app/` entry that is not
    Cairn's own package, dist-info, or entry scripts are third-party files.
    Their names and archive metadata are still scanned; only their contents
    are skipped. Anything else, including unexpected layouts, is scanned.
    """
    if release_root is None:
        return False
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if len(parts) < 3 or ".." in parts or parts[0] != release_root:
        return False
    if parts[1] == "python":
        return True
    if parts[1] == "app":
        return not (parts[2] in OWNED_APP_ENTRIES or OWNED_DIST_INFO.match(parts[2]))
    return False


def is_checksum_manifest(name: str, release_root: str | None) -> bool:
    parts = PurePosixPath(name.replace("\\", "/")).parts
    return release_root is not None and parts == (release_root, "SHA256SUMS")


def scan_member(
    data: bytes,
    name: str,
    label: str,
    patterns: list[re.Pattern[str]],
    findings: set[str],
    depth: int,
    release_root: str | None,
) -> None:
    """Scan one archive member; drop SHA256SUMS lines that name bundled third-party files."""
    if depth == 0 and is_checksum_manifest(name, release_root):
        root = release_root
        lines = data.decode("utf-8", errors="replace").split("\n")
        kept = [
            ""
            if len(line.split("  ", 1)) == 2
            and is_vendored_member(f"{root}/{line.split('  ', 1)[1]}", release_root)
            else line
            for line in lines
        ]
        data = "\n".join(kept).encode("utf-8")
    scan_blob(data, label, patterns, findings, depth + 1)


def decompress(data: bytes) -> bytes:
    try:
        if data.startswith(b"\x1f\x8b"):
            return gzip.decompress(data)
        if data.startswith(b"BZh"):
            return bz2.decompress(data)
        return lzma.decompress(data)
    except (OSError, EOFError, ValueError, lzma.LZMAError) as error:
        raise ScanError from error


def scan_blob(
    data: bytes,
    display: str,
    patterns: list[re.Pattern[str]],
    findings: set[str],
    depth: int = 0,
    release_root: str | None = None,
) -> None:
    """Scan data. `release_root` marks a trusted packaged release artifact (see release_root_for)."""
    looks_like_zip = data.startswith(ZIP_SIGNATURES)
    looks_like_tar = (
        len(data) > 262 and data[257:262] == TAR_SIGNATURE
    ) or data.startswith(COMPRESSED_TAR_SIGNATURES)
    if depth >= 16 and (looks_like_zip or looks_like_tar):
        raise ScanError

    if looks_like_zip:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                scan_binary(archive.comment, f"{display}-comment", patterns, findings)
                for index, member in enumerate(archive.infolist(), start=1):
                    label = safe_member_label(display, index)
                    vendored = depth == 0 and is_vendored_member(
                        member.filename, release_root
                    )
                    if not vendored:
                        scan_text(member.filename, f"{label}-name", patterns, findings)
                    scan_binary(member.comment, f"{label}-comment", patterns, findings)
                    scan_binary(member.extra, f"{label}-extra", patterns, findings)
                    local_extra = zip_local_extra(data, member, archive.start_dir)
                    if local_extra != member.extra:
                        scan_binary(
                            local_extra, f"{label}-local-extra", patterns, findings
                        )
                    if member.is_dir() or vendored:
                        continue
                    scan_member(
                        archive.read(member),
                        member.filename,
                        label,
                        patterns,
                        findings,
                        depth,
                        release_root,
                    )
                return
        except Exception as error:
            raise ScanError from error

    if looks_like_tar:
        try:
            archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")  # noqa: SIM115 - closed by the with block below
        except Exception as error:
            if not data.startswith(COMPRESSED_TAR_SIGNATURES):
                raise ScanError from error
            # A compressed stream that is not a tar (such as a .pkl.gz data
            # file): scan the decompressed payload instead.
            scan_blob(
                decompress(data), f"{display}!stream", patterns, findings, depth + 1
            )
            return
        try:
            with archive:
                for index, member in enumerate(archive.getmembers(), start=1):
                    label = safe_member_label(display, index)
                    vendored = depth == 0 and is_vendored_member(
                        member.name, release_root
                    )
                    if not vendored:
                        scan_text(member.name, f"{label}-name", patterns, findings)
                    scan_text(member.linkname, f"{label}-link", patterns, findings)
                    scan_text(member.uname, f"{label}-user", patterns, findings)
                    scan_text(member.gname, f"{label}-group", patterns, findings)
                    for key, value in member.pax_headers.items():
                        scan_text(key, f"{label}-pax-key", patterns, findings)
                        scan_text(value, f"{label}-pax-value", patterns, findings)
                    if not member.isfile() or vendored:
                        continue
                    stream = archive.extractfile(member)
                    if stream is not None:
                        scan_member(
                            stream.read(),
                            member.name,
                            label,
                            patterns,
                            findings,
                            depth,
                            release_root,
                        )
                return
        except Exception as error:
            raise ScanError from error

    if b"\x00" not in data:
        try:
            scan_text(data.decode("utf-8"), display, patterns, findings)
            return
        except UnicodeDecodeError:
            pass
    scan_binary(data, display, patterns, findings)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: check_release_text.py REPOSITORY [PATH ...]", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).resolve()
    patterns = configured_patterns()
    files = source_files(root)
    dist = root / "dist"
    if dist.is_dir():
        files.update(path for path in dist.rglob("*") if path.is_file())
    for raw_path in sys.argv[2:]:
        path = Path(raw_path)
        if not path.is_absolute():
            path = root / path
        add_input(path, root, files)

    findings: set[str] = set()
    for path in sorted(files, key=lambda item: display_path(item, root)):
        display = display_path(path, root)
        try:
            scan_blob(
                path.read_bytes(),
                display,
                patterns,
                findings,
                release_root=release_root_for(path, root),
            )
        except (OSError, ScanError):
            print(f"cannot scan {display}:1", file=sys.stderr)
            return 2
    for finding in sorted(findings):
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
