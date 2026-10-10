import os
import subprocess
import tarfile
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
