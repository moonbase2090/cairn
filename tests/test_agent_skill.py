from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from cairn import skill_install
from cairn.cli import build_parser, main


def source_skill() -> Path:
    return Path(__file__).resolve().parents[1] / "skills" / "cairn" / "SKILL.md"


def test_skill_mentions_every_top_level_command() -> None:
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    skill = source_skill().read_text()
    missing = [
        command for command in subparsers.choices if f"cairn {command}" not in skill
    ]
    assert not missing, (
        f"Document these top-level cairn commands in the skill: {missing}"
    )


def test_packaged_skill_matches_source() -> None:
    packaged = Path(skill_install.files("cairn").joinpath("agent_skill.md"))
    assert packaged.read_bytes() == source_skill().read_bytes()


def test_install_and_check_selected_agent(tmp_path: Path) -> None:
    codex = tmp_path / ".codex" / "skills" / "cairn" / "SKILL.md"
    check = skill_install.install_skill(tmp_path, "codex", check=True)
    assert [result.status for result in check] == ["missing", "missing"]
    assert not codex.exists()

    installed = skill_install.install_skill(tmp_path, "codex")
    assert [result.status for result in installed] == ["installed", "installed"]
    assert codex.read_bytes() == source_skill().read_bytes()
    assert (
        tmp_path / ".agents" / "skills" / "cairn" / "SKILL.md"
    ).read_bytes() == source_skill().read_bytes()

    current = skill_install.install_skill(tmp_path, "codex")
    assert [result.status for result in current] == [
        "already current",
        "already current",
    ]


def test_conflicting_file_blocks_all_selected_writes_unless_forced(
    tmp_path: Path,
) -> None:
    codex_dir = tmp_path / ".codex" / "skills" / "cairn"
    codex_dir.mkdir(parents=True)
    skill_file = codex_dir / "SKILL.md"
    skill_file.write_text("local edit\n")
    note = codex_dir / "notes.md"
    note.write_text("keep this\n")

    try:
        skill_install.install_skill(tmp_path, "codex")
    except skill_install.SkillInstallError as error:
        assert "use --force" in str(error)
    else:
        raise AssertionError("a different skill file must be preserved")
    assert skill_file.read_text() == "local edit\n"
    assert not (tmp_path / ".agents" / "skills" / "cairn" / "SKILL.md").exists()

    forced = skill_install.install_skill(tmp_path, "codex", force=True)
    assert [result.status for result in forced] == ["replaced", "installed"]
    assert skill_file.read_bytes() == source_skill().read_bytes()
    assert note.read_text() == "keep this\n"


def test_detected_installs_only_agents_with_config_directories_or_commands(
    tmp_path: Path,
) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".claude").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cursor = bin_dir / "cursor-agent"
    cursor.write_text("#!/bin/sh\nexit 0\n")
    cursor.chmod(0o755)

    targets, muse_detected = skill_install.detected_targets(
        tmp_path, tmp_path / ".config", str(bin_dir)
    )
    assert [(target.label, target.skill_dir) for target in targets] == [
        ("codex", tmp_path / ".codex" / "skills" / "cairn"),
        ("claude", tmp_path / ".claude" / "skills" / "cairn"),
        ("cursor", tmp_path / ".cursor" / "skills" / "cairn"),
    ]
    assert not muse_detected


def test_skills_install_is_gated_and_json_is_supported(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CAIRN_EXPERIMENTAL_SKILLS", raising=False)
    assert main(["skills", "install", "--agent", "codex"]) == 2
    assert "off by default" in capsys.readouterr().err

    monkeypatch.setenv("CAIRN_EXPERIMENTAL_SKILLS", "1")
    assert main(["skills", "install", "--agent", "shared", "--check", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"status": "missing"' in output
    assert not (tmp_path / ".agents" / "skills" / "cairn" / "SKILL.md").exists()


def test_muse_list_accepts_the_managed_user_skill_path(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_run(command, **kwargs):
        assert command == ["muse", "skills", "list", "--source", "user", "--json"]
        assert kwargs["env"]["HOME"] == str(tmp_path)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"skills":[{"id":"cairn","path":"$CONFIG_DIR/skills/cairn/SKILL.md"}]}',
            stderr="",
        )

    monkeypatch.setattr(skill_install.subprocess, "run", fake_run)
    assert (
        skill_install.muse_skill_path(tmp_path) == "$CONFIG_DIR/skills/cairn/SKILL.md"
    )


def _mock_muse_list(monkeypatch, home: Path, stdout: str, *, stderr="", code=0):
    def fake_run(command, **kwargs):
        assert command == ["muse", "skills", "list", "--source", "user", "--json"]
        assert kwargs["env"]["HOME"] == str(home)
        return subprocess.CompletedProcess(command, code, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(skill_install.subprocess, "run", fake_run)


@pytest.mark.parametrize(
    ("code", "stdout", "stderr", "message"),
    [
        (1, "fallback detail", "", "muse skills list failed: fallback detail"),
        (1, "ignored", "stderr detail", "muse skills list failed: stderr detail"),
    ],
)
def test_muse_list_reports_command_failure(
    tmp_path: Path, monkeypatch, code, stdout, stderr, message
):
    _mock_muse_list(monkeypatch, tmp_path, stdout, stderr=stderr, code=code)
    with pytest.raises(skill_install.SkillInstallError, match=message):
        skill_install.muse_skill_path(tmp_path)


def test_muse_list_reports_missing_command(tmp_path: Path, monkeypatch):
    def missing_command(*args, **kwargs):
        raise FileNotFoundError("muse")

    monkeypatch.setattr(skill_install.subprocess, "run", missing_command)
    with pytest.raises(skill_install.SkillInstallError, match="run muse skills list"):
        skill_install.muse_skill_path(tmp_path)


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        ("not JSON", "parse JSON"),
        ("[]", "did not return a JSON object"),
        ('{"skills": {}}', "did not contain a skills array"),
        ('{"skills": [{"id": "cairn"}]}', "without a skill path"),
    ],
)
def test_muse_list_rejects_invalid_responses(
    tmp_path: Path, monkeypatch, stdout, message
):
    _mock_muse_list(monkeypatch, tmp_path, stdout)
    with pytest.raises(skill_install.SkillInstallError, match=message):
        skill_install.muse_skill_path(tmp_path)


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ('{"skills": [null]}', None),
        ('{"skills": [], "diagnostics": {}}', None),
        ('{"skills": [], "diagnostics": [null, {"path": "other"}]}', None),
        (
            '{"skills": [], "diagnostics": [{"path": "$CONFIG_DIR/skills/cairn"}]}',
            "$CONFIG_DIR/skills/cairn",
        ),
        (
            '{"skills": [{"name": "cairn", "path": "$CONFIG_DIR/skills/cairn"}]}',
            "$CONFIG_DIR/skills/cairn",
        ),
    ],
)
def test_muse_list_checks_skill_entries_and_diagnostics(
    tmp_path: Path, monkeypatch, stdout, expected
):
    _mock_muse_list(monkeypatch, tmp_path, stdout)
    assert skill_install.muse_skill_path(tmp_path) == expected


def test_detected_install_reports_when_no_agent_is_available(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(skill_install, "detected_targets", lambda *args: ([], False))
    assert skill_install.install_skill(
        tmp_path,
        "detected",
        path_value="",
        config_home=tmp_path / ".config",
    ) == [skill_install.InstallResult("detected", "no supported agents detected", "")]


def test_check_reports_current_and_replaceable_file_targets(tmp_path: Path):
    codex_dir = tmp_path / ".codex" / "skills" / "cairn"
    shared_dir = tmp_path / ".agents" / "skills" / "cairn"
    codex_dir.mkdir(parents=True)
    shared_dir.mkdir(parents=True)
    (codex_dir / "SKILL.md").write_bytes(source_skill().read_bytes())
    (shared_dir / "SKILL.md").write_text("local edit\n")

    results = skill_install.install_skill(tmp_path, "codex", check=True, force=True)

    assert [(result.agent, result.status) for result in results] == [
        ("codex", "current"),
        ("shared", "would replace"),
    ]


@pytest.mark.parametrize(
    ("path", "force", "status"),
    [
        (None, False, "missing"),
        ("$CONFIG_DIR/skills/cairn/SKILL.md", False, "present; left in place"),
        ("$CONFIG_DIR/skills/cairn/SKILL.md", True, "would replace"),
    ],
)
def test_muse_check_reports_presence_and_force_status(
    tmp_path: Path, monkeypatch, path, force, status
):
    monkeypatch.setattr(skill_install, "muse_skill_path", lambda home: path)
    assert skill_install.install_skill(tmp_path, "muse", check=True, force=force) == [
        skill_install.InstallResult(
            "muse", status, path or "$CONFIG_DIR/skills/cairn/SKILL.md"
        )
    ]


@pytest.mark.parametrize(
    ("path", "force", "status", "calls"),
    [
        (None, False, "installed", [False]),
        (
            "$CONFIG_DIR/skills/cairn/SKILL.md",
            False,
            "already present; left in place",
            [],
        ),
        (
            "$CONFIG_DIR/skills/cairn/SKILL.md",
            True,
            "installed",
            [True],
        ),
    ],
)
def test_muse_install_respects_presence_and_force(
    tmp_path: Path, monkeypatch, path, force, status, calls
):
    installs = []
    monkeypatch.setattr(skill_install, "muse_skill_path", lambda home: path)
    monkeypatch.setattr(
        skill_install, "install_muse", lambda home, forced: installs.append(forced)
    )

    result = skill_install.install_skill(tmp_path, "muse", force=force)

    assert result == [
        skill_install.InstallResult(
            "muse", status, path or "$CONFIG_DIR/skills/cairn/SKILL.md"
        )
    ]
    assert installs == calls
