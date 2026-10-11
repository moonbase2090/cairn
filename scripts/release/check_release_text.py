#!/usr/bin/env python3
"""Check public release inputs for generic and locally configured deny patterns."""

from __future__ import annotations

import ipaddress
import io
import os
import re
import sys
import tarfile
import zipfile
from pathlib import Path


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
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
ASCII_STRINGS = re.compile(rb"[\x20-\x7e\t]{4,}")
UTF16_STRINGS = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
TAR_SIGNATURE = b"ustar"
COMPRESSED_TAR_SIGNATURES = (b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00")
GENERIC_PATTERNS = (
    re.compile(r"DESKTOP-[A-Z0-9]+", re.IGNORECASE),
    re.compile(r"\.local\b", re.IGNORECASE),
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
                print(f"invalid {source} release denylist pattern on line {number}", file=sys.stderr)
                raise SystemExit(2)
    return patterns


def is_private_ip(text: str) -> bool:
    for candidate in (IPV4_CANDIDATE, IPV6_CANDIDATE):
        for match in candidate.finditer(text):
            try:
                address = ipaddress.ip_address(match.group(0))
            except ValueError:
                continue
            if any(address in network for network in PRIVATE_NETWORKS if network.version == address.version):
                return True
    return False


def line_matches(text: str, patterns: list[re.Pattern[str]]) -> bool:
    if any(pattern.search(text) for pattern in patterns):
        return True
    if is_private_ip(text):
        return True
    return any(match.group(0).casefold() != ALLOWED_EMAIL.casefold() for match in EMAIL.finditer(text))


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


def scan_blob(
    data: bytes,
    display: str,
    patterns: list[re.Pattern[str]],
    findings: set[str],
    depth: int = 0,
) -> None:
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
                    scan_text(member.filename, f"{label}-name", patterns, findings)
                    scan_binary(member.comment, f"{label}-comment", patterns, findings)
                    if member.is_dir():
                        continue
                    scan_blob(archive.read(member), label, patterns, findings, depth + 1)
                return
        except Exception as error:
            raise ScanError from error

    if looks_like_tar:
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
                for index, member in enumerate(archive.getmembers(), start=1):
                    label = safe_member_label(display, index)
                    scan_text(member.name, f"{label}-name", patterns, findings)
                    scan_text(member.linkname, f"{label}-link", patterns, findings)
                    scan_text(member.uname, f"{label}-user", patterns, findings)
                    scan_text(member.gname, f"{label}-group", patterns, findings)
                    for key, value in member.pax_headers.items():
                        scan_text(key, f"{label}-pax-key", patterns, findings)
                        scan_text(value, f"{label}-pax-value", patterns, findings)
                    if not member.isfile():
                        continue
                    stream = archive.extractfile(member)
                    if stream is not None:
                        scan_blob(stream.read(), label, patterns, findings, depth + 1)
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
            scan_blob(path.read_bytes(), display, patterns, findings)
        except (OSError, ScanError):
            print(f"cannot scan {display}:1", file=sys.stderr)
            return 2
    for finding in sorted(findings):
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
