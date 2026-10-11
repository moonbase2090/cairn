import os
import subprocess
import tarfile
import zipfile
from pathlib import Path


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
    archive_path = tmp_path / "release-artifact.zip"
    private_path = b"/Users/fixture/private/local-header-path"
    extra_field = b"\xfe\xca" + len(private_path).to_bytes(2, "little") + private_path
    member = zipfile.ZipInfo("release-notes.txt")
    member.extra = extra_field
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(member, "Public release notes")
        member.extra = b""

    archive_bytes = archive_path.read_bytes()
    local_name_size = int.from_bytes(archive_bytes[26:28], "little")
    local_extra_size = int.from_bytes(archive_bytes[28:30], "little")
    local_extra_start = 30 + local_name_size
    assert archive_bytes[local_extra_start:local_extra_start + local_extra_size] == extra_field
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
