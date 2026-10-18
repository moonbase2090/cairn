import importlib.util
import io
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

SCANNER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "release" / "check_release_text.py"
SCANNER_SPEC = importlib.util.spec_from_file_location("check_release_text", SCANNER_PATH)
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


def test_release_guard_rejects_private_path_in_zip_local_header_extra(tmp_path: Path) -> None:
    archive_path = local_extra_archive(tmp_path)

    archive_bytes = archive_path.read_bytes()
    extra_field_size = int.from_bytes(archive_bytes[28:30], "little")
    local_name_size = int.from_bytes(archive_bytes[26:28], "little")
    local_extra_start = 30 + local_name_size
    assert b"/Users/fixture/private/local-header-path" in (
        archive_bytes[local_extra_start:local_extra_start + extra_field_size]
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


def test_configured_patterns_detects_local_and_environment_rules(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    denylist = tmp_path / ".config" / "moonbase" / "release-denylist.txt"
    denylist.parent.mkdir(parents=True)
    denylist.write_text("LOCAL_RELEASE_MARKER\n\n")
    monkeypatch.setenv("RELEASE_DENYLIST", "CI_RELEASE_MARKER")

    patterns = scanner.configured_patterns()

    assert scanner.line_matches("artifact LOCAL_RELEASE_MARKER", patterns)
    assert scanner.line_matches("artifact CI_RELEASE_MARKER", patterns)


def test_configured_patterns_rejects_invalid_pattern_without_echoing_it(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("RELEASE_DENYLIST", "[")

    with pytest.raises(SystemExit) as error:
        scanner.configured_patterns()
    assert error.value.code == 2

    assert "invalid secret release denylist pattern on line 1" in capsys.readouterr().err


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
    scanner.scan_blob(archive_bytes.getvalue(), "nested.tar.gz", scanner.GENERIC_PATTERNS, findings)

    assert findings == {
        "nested.tar.gz!member-1-link:1",
        "nested.tar.gz!member-2-pax-value:1",
        "nested.tar.gz!member-2:1",
    }


def test_scan_blob_finds_zip_comments_directory_names_and_local_extra(tmp_path: Path) -> None:
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
    scanner.scan_blob(archive_bytes.getvalue(), "metadata.zip", scanner.GENERIC_PATTERNS, findings)
    assert findings == {
        "metadata.zip-comment:1",
        "metadata.zip!member-1-comment:1",
        "metadata.zip!member-1-name:1",
        "metadata.zip!member-2-comment:1",
    }

    local_findings: set[str] = set()
    scanner.scan_blob(archive_path.read_bytes(), "local-extra.zip", scanner.GENERIC_PATTERNS, local_findings)
    assert local_findings == {"local-extra.zip!member-1-local-extra:1"}


def test_scan_blob_handles_utf8_binary_and_fail_closed_archives() -> None:
    findings: set[str] = set()
    scanner.scan_blob(b"release at /Users/fixture/private", "notes.txt", scanner.GENERIC_PATTERNS, findings)
    scanner.scan_blob(b"\x00\xff/Users/fixture/binary", "notes.bin", scanner.GENERIC_PATTERNS, findings)
    assert findings == {"notes.txt:1", "notes.bin:1"}

    with pytest.raises(scanner.ScanError):
        scanner.scan_blob(b"PK\x03\x04broken", "broken.zip", scanner.GENERIC_PATTERNS, set())
    with pytest.raises(scanner.ScanError):
        scanner.scan_blob(b"PK\x03\x04", "too-deep.zip", scanner.GENERIC_PATTERNS, set(), depth=16)


def test_scan_blob_scans_compressed_streams_that_are_not_tar() -> None:
    import bz2
    import gzip
    import lzma

    clean: set[str] = set()
    scanner.scan_blob(gzip.compress(b"\x80\x04public pickle"), "data.pkl.gz", scanner.GENERIC_PATTERNS, clean)
    assert clean == set()

    findings: set[str] = set()
    payload = b"release from /Users/fixture/private"
    scanner.scan_blob(gzip.compress(payload), "notes.gz", scanner.GENERIC_PATTERNS, findings)
    scanner.scan_blob(bz2.compress(payload), "notes.bz2", scanner.GENERIC_PATTERNS, findings)
    scanner.scan_blob(lzma.compress(payload), "notes.xz", scanner.GENERIC_PATTERNS, findings)
    assert findings == {"notes.gz!stream:1", "notes.bz2!stream:1", "notes.xz!stream:1"}

    with pytest.raises(scanner.ScanError):
        scanner.scan_blob(b"\x1f\x8b\x08broken", "broken.gz", scanner.GENERIC_PATTERNS, set())


def release_archive(files: dict[str, bytes]) -> bytes:
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return archive_bytes.getvalue()


def test_is_vendored_member_matches_only_bundled_third_party_paths() -> None:
    root = "cairn-0.15.5-x86_64-unknown-linux-gnu"
    assert scanner.is_vendored_member(f"{root}/python/lib/python3.12/smtplib.py", root)
    assert scanner.is_vendored_member(f"{root}/app/numpy/lib/_datasource.py", root)
    assert scanner.is_vendored_member(f"{root}/app/numpy-2.5.4.dist-info/METADATA", root)
    assert scanner.is_vendored_member(f"{root}\\python\\Lib\\site.py", root)

    assert not scanner.is_vendored_member(f"{root}/python/lib/os.py", None)
    assert not scanner.is_vendored_member(f"{root}/app/cairn/cli.py", root)
    assert not scanner.is_vendored_member(f"{root}/app/cairn-0.15.5.dist-info/METADATA", root)
    assert not scanner.is_vendored_member(f"{root}/app/entries/run_cairn.py", root)
    assert not scanner.is_vendored_member(f"{root}/install.sh", root)
    assert not scanner.is_vendored_member(f"{root}/python/../install.sh", root)
    assert not scanner.is_vendored_member("cairn-9.9.9-other/python/lib/os.py", root)
    assert not scanner.is_vendored_member("python/lib/os.py", root)


def test_release_root_for_trusts_only_packaged_artifacts_in_dist(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (tmp_path / "docs").mkdir()
    linux = dist / "cairn-0.15.5-x86_64-unknown-linux-gnu.tar.gz"
    windows = dist / "cairn-0.15.5-x86_64-pc-windows-msvc.zip"
    for path in (linux, windows):
        path.write_bytes(b"")
    assert scanner.release_root_for(linux, tmp_path) == "cairn-0.15.5-x86_64-unknown-linux-gnu"
    assert scanner.release_root_for(windows, tmp_path) == "cairn-0.15.5-x86_64-pc-windows-msvc"
    assert scanner.release_root_for(tmp_path / "docs" / linux.name, tmp_path) is None
    assert scanner.release_root_for(dist / "cairn-0.15.5.tar.gz", tmp_path) is None
    assert scanner.release_root_for(dist / "notes-0.15.5-x86_64.tar.gz", tmp_path) is None
    assert scanner.release_root_for(dist / "SHA256SUMS", tmp_path) is None


def bundled_release_archive(root: str) -> bytes:
    return release_archive({
        f"{root}/python/lib/python3.12/smtplib.py": b"example: user@example.invalid",
        f"{root}/python/lib/config/Setup.local": b"",
        f"{root}/app/numpy/lib/_datasource.py": b"cache under /home/fixture/data",
        f"{root}/app/cairn/cli.py": b"built in /home/fixture/checkout",
        f"{root}/app/cairn-0.15.5.dist-info/direct_url.json": b'{"url": "file:///Users/fixture/cairn"}',
        f"{root}/install.sh": b"contact someone@example.invalid",
        f"{root}/SHA256SUMS": (
            b"aa  python/lib/config/Setup.local\n"
            b"bb  app/cairn/notes.local\n"
        ),
    })


def test_scan_blob_skips_bundled_third_party_content_but_scans_cairn_files() -> None:
    root = "cairn-0.15.5-x86_64-unknown-linux-gnu"
    findings: set[str] = set()
    scanner.scan_blob(bundled_release_archive(root), "release.tar.gz", scanner.GENERIC_PATTERNS, findings,
                      release_root=root)

    assert findings == {
        "release.tar.gz!member-4:1",
        "release.tar.gz!member-5:1",
        "release.tar.gz!member-6:1",
        "release.tar.gz!member-7:2",
    }


def test_untrusted_archives_are_scanned_in_full() -> None:
    root = "cairn-0.15.5-x86_64-unknown-linux-gnu"
    untrusted: set[str] = set()
    scanner.scan_blob(bundled_release_archive(root), "docs.tar.gz", scanner.GENERIC_PATTERNS, untrusted)
    mismatched: set[str] = set()
    scanner.scan_blob(bundled_release_archive(root), "other.tar.gz", scanner.GENERIC_PATTERNS, mismatched,
                      release_root="cairn-0.15.5-x86_64-apple-darwin")
    expected = {f"!member-{n}:1" for n in (1, 3, 4, 5, 6)} | {"!member-2-name:1", "!member-7:1", "!member-7:2"}
    assert untrusted == {"docs.tar.gz" + item for item in expected}
    assert mismatched == {"other.tar.gz" + item for item in expected}


def test_main_trusts_only_dist_release_artifacts(tmp_path: Path, monkeypatch, capsys) -> None:
    root = "cairn-0.15.5-x86_64-unknown-linux-gnu"
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / f"{root}.tar.gz").write_bytes(release_archive({
        f"{root}/python/lib/python3.12/smtplib.py": b"example: user@example.invalid",
    }))
    (tmp_path / "bundle").mkdir()
    (tmp_path / "bundle" / f"{root}.tar.gz").write_bytes(release_archive({
        f"{root}/python/lib/python3.12/smtplib.py": b"example: user@example.invalid",
    }))
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.delenv("RELEASE_DENYLIST", raising=False)
    monkeypatch.setattr(sys, "argv", ["check_release_text.py", str(tmp_path), "dist", "bundle"])
    assert scanner.main() == 1
    assert capsys.readouterr().out.split() == [f"bundle/{root}.tar.gz!member-1:1"]


def test_scan_blob_scans_vendored_paths_inside_nested_archives() -> None:
    root = "cairn-0.15.5-x86_64-unknown-linux-gnu"
    inner = release_archive({f"{root}/python/leak.txt": b"/Users/fixture/private"})
    findings: set[str] = set()
    scanner.scan_blob(inner, "outer.tar.gz", scanner.GENERIC_PATTERNS, findings, depth=1, release_root=root)
    assert findings == {"outer.tar.gz!member-1:1"}


def test_local_pattern_flags_hostnames_not_dot_local_directories() -> None:
    patterns = list(scanner.GENERIC_PATTERNS)
    assert scanner.line_matches("ssh build-host.local", patterns)
    assert not scanner.line_matches("prefix=${HOME}/.local", patterns)
    assert not scanner.line_matches('(Join-Path $env:USERPROFILE ".local")', patterns)


def test_url_userinfo_is_not_an_email_address() -> None:
    patterns = list(scanner.GENERIC_PATTERNS)
    assert not scanner.line_matches('url = "postgresql://cairn:password@db.example/cairn"', patterns)
    assert scanner.line_matches("mail someone@example.invalid", patterns)
    assert scanner.line_matches("mailto:someone@example.invalid", patterns)


def test_main_reports_findings_usage_and_missing_inputs(tmp_path: Path, monkeypatch, capsys) -> None:
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
