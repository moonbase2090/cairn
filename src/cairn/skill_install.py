"""Install the bundled Cairn Agent Skill for supported coding agents."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path


SKILL = files("cairn").joinpath("agent_skill.md").read_bytes()


@dataclass(frozen=True)
class FileTarget:
    agent: str
    label: str
    skill_dir: Path


@dataclass(frozen=True)
class InstallResult:
    agent: str
    status: str
    path: str


class SkillInstallError(ValueError):
    """A safe, user-facing skill installation error."""


def command_on_path(path_value: str | None, names: tuple[str, ...]) -> bool:
    if not path_value:
        return False
    for directory in path_value.split(os.pathsep):
        for name in names:
            candidate = Path(directory) / name
            if os.name == "nt":
                if candidate.is_file() or any(
                    candidate.with_suffix(suffix).is_file()
                    for suffix in (".exe", ".cmd", ".bat")
                ):
                    return True
            elif candidate.is_file() and os.access(candidate, os.X_OK):
                return True
    return False


def detected_targets(
    home: Path,
    config_home: Path,
    path_value: str | None,
) -> tuple[list[FileTarget], bool]:
    """Return only destinations for agents with a config directory or CLI."""
    targets: list[FileTarget] = []
    codex_dir = home / ".codex"
    shared_dir = home / ".agents"
    codex_detected = (
        codex_dir.is_dir()
        or shared_dir.is_dir()
        or command_on_path(path_value, ("codex",))
    )
    if codex_detected:
        if codex_dir.is_dir():
            targets.append(FileTarget("codex", "codex", codex_dir / "skills" / "cairn"))
        if shared_dir.is_dir():
            targets.append(FileTarget("codex", "shared", shared_dir / "skills" / "cairn"))
        if not codex_dir.is_dir() and not shared_dir.is_dir():
            targets.append(FileTarget("codex", "codex", codex_dir / "skills" / "cairn"))

    for agent, label, config_name, commands in (
        ("claude", "claude", ".claude", ("claude",)),
        ("cursor", "cursor", ".cursor", ("cursor-agent", "cursor")),
        ("kiro", "kiro", ".kiro", ("kiro-cli", "kiro")),
    ):
        config_dir = home / config_name
        if config_dir.is_dir() or command_on_path(path_value, commands):
            targets.append(FileTarget(agent, label, config_dir / "skills" / "cairn"))

    muse_detected = (
        (config_home / "muse").is_dir()
        or (home / ".muse").is_dir()
        or command_on_path(path_value, ("muse",))
    )
    return targets, muse_detected


def file_targets(home: Path, agent: str) -> list[FileTarget]:
    roots = {
        "codex": (("codex", ".codex"), ("shared", ".agents")),
        "claude": (("claude", ".claude"),),
        "cursor": (("cursor", ".cursor"),),
        "kiro": (("kiro", ".kiro"),),
        "shared": (("shared", ".agents"),),
    }
    if agent == "all":
        names = ("codex", "claude", "cursor", "kiro")
    elif agent in roots:
        names = (agent,)
    else:
        return []
    targets = []
    for name in names:
        for label, directory in roots[name]:
            targets.append(FileTarget(name, label, home / directory / "skills" / "cairn"))
    return targets


def inspect_file(skill_dir: Path) -> str:
    path = skill_dir / "SKILL.md"
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return "missing"
    except OSError as error:
        raise SkillInstallError(f"cannot read {path}: {error}") from error
    return "current" if content == SKILL else "different"


def muse_environment(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.setdefault("XDG_CONFIG_HOME", str(home / ".config"))
    return env


def muse_skill_path(home: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["muse", "skills", "list", "--source", "user", "--json"],
            check=False,
            capture_output=True,
            text=True,
            env=muse_environment(home),
        )
    except OSError as error:
        raise SkillInstallError(
            "run muse skills list --source user --json (install Muse or select another agent)"
        ) from error
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SkillInstallError(f"muse skills list failed: {detail}")
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise SkillInstallError("parse JSON from muse skills list --source user --json") from error
    if not isinstance(response, dict):
        raise SkillInstallError("Muse skill list did not return a JSON object")
    skills = response.get("skills")
    if not isinstance(skills, list):
        raise SkillInstallError("Muse skill list did not contain a skills array")
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        if skill.get("id") == "cairn" or skill.get("name") == "cairn":
            path = skill.get("path")
            if not isinstance(path, str):
                raise SkillInstallError("Muse listed cairn without a skill path")
            if path in ("$CONFIG_DIR/skills/cairn", "$CONFIG_DIR/skills/cairn/SKILL.md"):
                return path
    diagnostics = response.get("diagnostics", [])
    if not isinstance(diagnostics, list):
        diagnostics = []
    for diagnostic in diagnostics:
        if isinstance(diagnostic, dict) and diagnostic.get("path") in (
            "$CONFIG_DIR/skills/cairn",
            "$CONFIG_DIR/skills/cairn/SKILL.md",
        ):
            return diagnostic["path"]
    return None


def install_muse(home: Path, force: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="cairn-skill-source-") as temp:
        source = Path(temp)
        (source / "SKILL.md").write_bytes(SKILL)
        command = [
            "muse", "skills", "install", str(source),
            "--scope", "user", "--name", "cairn",
        ]
        if force:
            command.append("--force")
        command.append("--json")
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=muse_environment(home),
            )
        except OSError as error:
            raise SkillInstallError("run muse skills install") from error
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise SkillInstallError(f"muse skills install failed: {detail}")


def preflight_targets(
    targets: list[FileTarget],
    force: bool,
) -> list[tuple[FileTarget, str]]:
    states = [(target, inspect_file(target.skill_dir)) for target in targets]
    conflicts = [
        str(target.skill_dir / "SKILL.md")
        for target, state in states
        if state == "different" and not force
    ]
    if conflicts:
        raise SkillInstallError(
            f"existing Cairn skill differs at {', '.join(conflicts)}; use --force to replace it"
        )
    return states


def write_skill(skill_dir: Path, force: bool) -> None:
    destination = skill_dir / "SKILL.md"
    if destination.exists() and not force:
        state = inspect_file(skill_dir)
        if state == "current":
            return
        if state == "different":
            raise SkillInstallError(
                f"{destination} already contains a different skill; use --force to replace it"
            )
    temporary_path: Path | None = None
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".SKILL.md.", suffix=".tmp", dir=skill_dir, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(SKILL)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
    except OSError as error:
        raise SkillInstallError(f"install {destination}: {error}") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def install_skill(
    home: Path,
    agent: str,
    *,
    check: bool = False,
    force: bool = False,
    path_value: str | None = None,
    config_home: Path | None = None,
) -> list[InstallResult]:
    """Install or inspect selected user-scope skill destinations."""
    config_home = config_home or Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    include_muse = agent in ("all", "muse")
    if agent == "detected":
        targets, include_muse = detected_targets(
            home,
            config_home,
            os.environ.get("PATH") if path_value is None else path_value,
        )
        if not targets and not include_muse:
            return [InstallResult("detected", "no supported agents detected", "")]
    else:
        targets = file_targets(home, agent)

    states = preflight_targets(targets, force)
    muse_path = muse_skill_path(home) if include_muse else None
    results = []
    if check:
        for target, state in states:
            status = (
                "current" if state == "current"
                else "would replace" if state == "different" and force
                else "missing"
            )
            results.append(
                InstallResult(target.label, status, str(target.skill_dir / "SKILL.md"))
            )
        if include_muse:
            status = (
                "would replace" if muse_path and force
                else "present; left in place" if muse_path
                else "missing"
            )
            results.append(
                InstallResult(
                    "muse", status, muse_path or "$CONFIG_DIR/skills/cairn/SKILL.md"
                )
            )
        return results

    if include_muse and (muse_path is None or force):
        install_muse(home, force)
    for target, state in states:
        if state != "current":
            write_skill(target.skill_dir, force)
        status = "already current" if state == "current" else "installed"
        if state == "different":
            status = "replaced"
        results.append(InstallResult(target.label, status, str(target.skill_dir / "SKILL.md")))
    if include_muse:
        status = "already present; left in place" if muse_path and not force else "installed"
        results.append(
            InstallResult(
                "muse", status, muse_path or "$CONFIG_DIR/skills/cairn/SKILL.md"
            )
        )
    return results
