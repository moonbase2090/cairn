import importlib.util
import io
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

SCANNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "release"
    / "check_release_text.py"
)
SCANNER_SPEC = importlib.util.spec_from_file_location(
    "check_release_text", SCANNER_PATH
)
assert SCANNER_SPEC is not None and SCANNER_SPEC.loader is not None
scanner = importlib.util.module_from_spec(SCANNER_SPEC)
SCANNER_SPEC.loader.exec_module(scanner)


def local_extra_archive(tmp_path: Path) -> Path:
    archive_path = tmp_path / "release-artifact.zip"
    private_path = b"/Users/fixture/private/local-header-path"
    extra_field = b"\xfe\xca" + len(private_path).to_bytes(2, "little") + private_path
    member = zipfile.ZipInfo("release-notes.txt")
    member.extra = extra_field
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(member, "Public release notes")
        member.extra = b""
    return archive_path


def test_release_guard_rejects_tar_link_and_pax_metadata(tmp_path: Path) -> None:
    archive_path = tmp_path / "release-artifact.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        symlink = tarfile.TarInfo("symlink-entry")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "/Users/fixture/secret"
        archive.addfile(symlink)

        hardlink = tarfile.TarInfo("hardlink-entry")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = r"C:\Users\fixture\secret"
        archive.addfile(hardlink)

        metadata = tarfile.TarInfo("metadata-entry")
        metadata.size = 0
        metadata.pax_headers = {"comment": "192.168.4.20"}
        archive.addfile(metadata)

    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "empty-home")
    Path(env["HOME"]).mkdir()
    env.pop("RELEASE_DENYLIST", None)
    scanner = Path(__file__).resolve().parents[1] / "scripts" / "check-release-text.sh"
    result = subprocess.run(
        [str(scanner), str(archive_path)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "release-artifact.tar.gz!member-1-link:1",
        "release-artifact.tar.gz!member-2-link:1",
        "release-artifact.tar.gz!member-3-pax-value:1",
    ]
    assert result.stderr == ""


def test_release_guard_rejects_zip_directory_names_and_comments(tmp_path: Path) -> None:
    archive_path = tmp_path / "release-artifact.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.comment = b"release destination /home/fixture/hidden"

        directory = zipfile.ZipInfo("/Users/fixture/private/")
        directory.comment = b"network=192.168.4.20"
        archive.writestr(directory, "")

        member = zipfile.ZipInfo("release-notes.txt")
        member.comment = b"contact=release@example.invalid"
        archive.writestr(member, "Public release notes")

    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "empty-home")
    Path(env["HOME"]).mkdir()
    env.pop("RELEASE_DENYLIST", None)
    scanner = Path(__file__).resolve().parents[1] / "scripts" / "check-release-text.sh"
    result = subprocess.run(
        [str(scanner), str(archive_path)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "release-artifact.zip!member-1-comment:1",
        "release-artifact.zip!member-1-name:1",
        "release-artifact.zip!member-2-comment:1",
        "release-artifact.zip-comment:1",
    ]
    assert result.stderr == ""


def test_release_guard_rejects_private_path_in_zip_extra_field(tmp_path: Path) -> None:
    archive_path = tmp_path / "release-artifact.zip"
    private_path = b"/Users/fixture/private/path"
    extra_field = b"\xfe\xca" + len(private_path).to_bytes(2, "little") + private_path
    member = zipfile.ZipInfo("release-notes.txt")
    member.extra = extra_field
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(member, "Public release notes")

    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "empty-home")
    Path(env["HOME"]).mkdir()
    env.pop("RELEASE_DENYLIST", None)
    scanner = Path(__file__).resolve().parents[1] / "scripts" / "check-release-text.sh"
    result = subprocess.run(
        [str(scanner), str(archive_path)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert result.stdout.splitlines() == ["release-artifact.zip!member-1-extra:1"]
    assert result.stderr == ""


def test_release_guard_rejects_private_path_in_zip_local_header_extra(
    tmp_path: Path,
) -> None:
    archive_path = local_extra_archive(tmp_path)

    archive_bytes = archive_path.read_bytes()
    extra_field_size = int.from_bytes(archive_bytes[28:30], "little")
    local_name_size = int.from_bytes(archive_bytes[26:28], "little")
    local_extra_start = 30 + local_name_size
    assert (
        b"/Users/fixture/private/local-header-path"
        in (archive_bytes[local_extra_start : local_extra_start + extra_field_size])
    )
    with zipfile.ZipFile(archive_path) as archive:
        info = archive.infolist()[0]
        assert info.extra == b""
        assert archive.read(info) == b"Public release notes"

    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "empty-home")
    Path(env["HOME"]).mkdir()
    env.pop("RELEASE_DENYLIST", None)
    scanner = Path(__file__).resolve().parents[1] / "scripts" / "check-release-text.sh"
    result = subprocess.run(
        [str(scanner), str(archive_path)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert result.stdout.splitlines() == ["release-artifact.zip!member-1-local-extra:1"]
    assert result.stderr == ""


def test_configured_patterns_detects_local_and_environment_rules(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    denylist = tmp_path / ".config" / "moonbase" / "release-denylist.txt"
    denylist.parent.mkdir(parents=True)
    denylist.write_text("LOCAL_RELEASE_MARKER\n\n")
    monkeypatch.setenv("RELEASE_DENYLIST", "CI_RELEASE_MARKER")

    patterns = scanner.configured_patterns()

    assert scanner.line_matches("artifact LOCAL_RELEASE_MARKER", patterns)
    assert scanner.line_matches("artifact CI_RELEASE_MARKER", patterns)


def test_configured_patterns_rejects_invalid_pattern_without_echoing_it(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("RELEASE_DENYLIST", "[")

    with pytest.raises(SystemExit) as error:
        scanner.configured_patterns()
    assert error.value.code == 2

    assert (
        "invalid secret release denylist pattern on line 1" in capsys.readouterr().err
    )


def test_private_ip_and_line_matching_cover_private_and_public_values() -> None:
    assert scanner.is_private_ip("service uses 10.2.3.4")
    assert scanner.is_private_ip("service uses fc00::1234")
    assert not scanner.is_private_ip("public service uses 8.8.8.8")
    assert not scanner.is_private_ip("invalid address 999.1.1.1")

    patterns = list(scanner.GENERIC_PATTERNS)
    assert scanner.line_matches("support at person@example.invalid", patterns)
    assert not scanner.line_matches(f"support at {scanner.ALLOWED_EMAIL}", patterns)
    assert not scanner.line_matches("public release text", patterns)


def test_source_files_selects_release_documents(tmp_path: Path) -> None:
    (tmp_path / "CHANGELOG.md").write_text("history")
    (tmp_path / "CHANGELOG-old.txt").write_text("older history")
    (tmp_path / "README.md").write_text("project readme")
    (tmp_path / "release_notes.md").write_text("release detail")
    (tmp_path / "docs" / "nested").mkdir(parents=True)
    (tmp_path / "docs" / "nested" / "guide.md").write_text("guide")
    (tmp_path / "release-notes").mkdir()
    (tmp_path / "release-notes" / "v1.txt").write_text("release")
    (tmp_path / "unrelated.md").write_text("not release input")

    found = scanner.source_files(tmp_path)

    assert {path.relative_to(tmp_path).as_posix() for path in found} == {
        "CHANGELOG.md",
        "CHANGELOG-old.txt",
        "README.md",
        "release_notes.md",
        "docs/nested/guide.md",
        "release-notes/v1.txt",
    }


def test_scan_blob_finds_tar_metadata_and_nested_text() -> None:
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
        link = tarfile.TarInfo("link-entry")
        link.type = tarfile.SYMTYPE
        link.linkname = "/Users/fixture/tar-link"
        archive.addfile(link)

        document = tarfile.TarInfo("release.txt")
        document.size = len(b"contact at someone@example.invalid")
        document.pax_headers = {"note": "192.168.2.20"}
        archive.addfile(document, io.BytesIO(b"contact at someone@example.invalid"))

    findings: set[str] = set()
    scanner.scan_blob(
        archive_bytes.getvalue(), "nested.tar.gz", scanner.GENERIC_PATTERNS, findings
    )

    assert findings == {
        "nested.tar.gz!member-1-link:1",
        "nested.tar.gz!member-2-pax-value:1",
        "nested.tar.gz!member-2:1",
    }


def test_scan_blob_finds_zip_comments_directory_names_and_local_extra(
    tmp_path: Path,
) -> None:
    archive_path = local_extra_archive(tmp_path)
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.comment = b"release from /Users/fixture/private"

        directory = zipfile.ZipInfo("private/Users/fixture/")
        directory.comment = b"network 192.168.2.20"
        archive.writestr(directory, "")

        member = zipfile.ZipInfo("release-notes.txt")
        member.comment = b"contact person@example.invalid"
        member.extra = b"\xfe\xca\x01\x00x"
        archive.writestr(member, "Public release notes")

    findings: set[str] = set()
    scanner.scan_blob(
        archive_bytes.getvalue(), "metadata.zip", scanner.GENERIC_PATTERNS, findings
    )
    assert findings == {
        "metadata.zip-comment:1",
        "metadata.zip!member-1-comment:1",
        "metadata.zip!member-1-name:1",
        "metadata.zip!member-2-comment:1",
    }

    local_findings: set[str] = set()
    scanner.scan_blob(
        archive_path.read_bytes(),
        "local-extra.zip",
        scanner.GENERIC_PATTERNS,
        local_findings,
    )
    assert local_findings == {"local-extra.zip!member-1-local-extra:1"}


def test_scan_blob_handles_utf8_binary_and_fail_closed_archives() -> None:
    findings: set[str] = set()
    scanner.scan_blob(
        b"release at /Users/fixture/private",
        "notes.txt",
        scanner.GENERIC_PATTERNS,
        findings,
    )
    scanner.scan_blob(
        b"\x00\xff/Users/fixture/binary",
        "notes.bin",
        scanner.GENERIC_PATTERNS,
        findings,
    )
    assert findings == {"notes.txt:1", "notes.bin:1"}

    with pytest.raises(scanner.ScanError):
        scanner.scan_blob(
            b"PK\x03\x04broken", "broken.zip", scanner.GENERIC_PATTERNS, set()
        )
    with pytest.raises(scanner.ScanError):
        scanner.scan_blob(
            b"PK\x03\x04", "too-deep.zip", scanner.GENERIC_PATTERNS, set(), depth=16
        )


def test_main_reports_findings_usage_and_missing_inputs(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("Contact person@example.invalid")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RELEASE_DENYLIST", raising=False)
    monkeypatch.setattr(sys, "argv", [str(SCANNER_PATH), str(root)])

    assert scanner.main() == 1
    assert capsys.readouterr().out.splitlines() == ["README.md:1"]

    monkeypatch.setattr(sys, "argv", [str(SCANNER_PATH)])
    assert scanner.main() == 2
    assert "usage:" in capsys.readouterr().err

    monkeypatch.setattr(sys, "argv", [str(SCANNER_PATH), str(root), "missing.txt"])
    with pytest.raises(SystemExit) as error:
        scanner.main()
    assert error.value.code == 2
    assert "cannot read missing.txt:1" in capsys.readouterr().err
