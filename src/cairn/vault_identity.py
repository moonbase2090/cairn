"""Stable logical identity shared by replicas of one Cairn vault."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import uuid


_IDENTITY_MIGRATION = "1"


@dataclass(frozen=True)
class VaultIdentity:
    vault_id: str
    name: str

    def __post_init__(self) -> None:
        if not self.vault_id.strip():
            raise ValueError("vault identity is missing its id")
        if not self.name.strip():
            raise ValueError("vault identity is missing its display name")


def safe_vault_name(value: object, fallback: str = "cairn") -> str:
    """Return a display label that never exposes a directory path."""
    candidate = str(value or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    candidate = "".join(char for char in candidate if char.isprintable()).strip()
    if candidate in {"", ".", ".."}:
        candidate = str(fallback or "cairn").strip().replace("\\", "/").rsplit("/", 1)[-1]
        candidate = "".join(char for char in candidate if char.isprintable()).strip()
    return candidate or "cairn"


def initial_vault_name(vault_dir: Path) -> str:
    """Choose a safe migration label from project metadata or the vault folder."""
    try:
        metadata = json.loads((vault_dir / "project.json").read_text())
    except (OSError, ValueError):
        metadata = {}
    if isinstance(metadata, dict):
        name = metadata.get("project") or metadata.get("name")
        if name:
            return safe_vault_name(name, vault_dir.name)
    return safe_vault_name(vault_dir.name, "cairn")


def ensure_vault_identity(
    get_meta: Callable[[str], str | None],
    set_meta: Callable[[str, str], None],
    vault_dir: Path,
) -> VaultIdentity:
    """Create the identity once while upgrading a vault with no identity marker."""
    marker = get_meta("vault_identity_migration")
    vault_id = get_meta("vault_id")
    name = get_meta("vault_name")

    if marker is None:
        if vault_id is None and name is None:
            vault_id = uuid.uuid4().hex
            name = initial_vault_name(vault_dir)
            set_meta("vault_id", vault_id)
            set_meta("vault_name", name)
        elif vault_id is None or name is None:
            raise RuntimeError("vault identity metadata is incomplete")
        set_meta("vault_identity_migration", _IDENTITY_MIGRATION)
    elif marker != _IDENTITY_MIGRATION:
        raise RuntimeError("vault identity metadata has an unsupported version")

    return load_vault_identity(get_meta)


def load_vault_identity(get_meta: Callable[[str], str | None]) -> VaultIdentity:
    """Read a migrated identity and fail closed if any part is missing or unsafe."""
    if get_meta("vault_identity_migration") != _IDENTITY_MIGRATION:
        raise RuntimeError("vault identity is missing; refusing to use this vault")
    vault_id = get_meta("vault_id")
    name = get_meta("vault_name")
    if not isinstance(vault_id, str) or not isinstance(name, str):
        raise RuntimeError("vault identity is incomplete; refusing to use this vault")
    safe_name = safe_vault_name(name)
    if safe_name != name:
        raise RuntimeError("vault display name is unsafe; refusing to use this vault")
    try:
        return VaultIdentity(vault_id, name)
    except ValueError as error:
        raise RuntimeError("vault identity is invalid; refusing to use this vault") from error


def split_legacy_cursor_peer(peer: str) -> tuple[str, str]:
    """Separate the token id prefix used by the pre-v4 sync cursor schema."""
    if peer.startswith("ct_") and ":" in peer:
        token_id, peer_id = peer.split(":", 1)
        if token_id and peer_id:
            return peer_id, token_id
    return peer, ""
