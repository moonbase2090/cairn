"""Offline admission checks for accidental credential disclosures."""
from __future__ import annotations

from collections import Counter
import math
import re


class SecretAdmissionError(ValueError):
    """A content entry contains a value matching a reviewed secret category."""

    def __init__(self, category: str):
        self.category = category
        super().__init__(
            f"secret-like {category} detected; remove credentials before storing this content"
        )


_PRIVATE_KEY_BEGIN = re.compile(
    r"-----BEGIN[ \t]+(?:RSA[ \t]+|EC[ \t]+|DSA[ \t]+|ENCRYPTED[ \t]+|"
    r"OPENSSH[ \t]+)?PRIVATE KEY-----",
    re.IGNORECASE,
)
_GITHUB_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])(?:github_pat_|ghp_|gho_|ghu_|ghs_|ghr_)"
    r"[A-Za-z0-9_]{8,}(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_AWS_ACCESS_KEY_ID = re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])")
_ASSIGNMENT = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.-]{0,79})\s*[\"']?\s*[:=]\s*"
    r"(?:(?i:Bearer)\s+)?"
    r"(?P<value>\"[^\r\n\"]*\"|'[^\r\n']*'|[^\s,;#]+)"
)
_CREDENTIAL_NAME = re.compile(
    r"(?:^|[_\-.])(?:password|passwd|secret|api[_-]?key|access[_-]?key[_-]?id|"
    r"access[_-]?token|token|authorization)(?:$|[_\-.])",
    re.IGNORECASE,
)
_OPAQUE_SPAN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9+/=_-]{24,}(?![A-Za-z0-9])")
_HEX = re.compile(r"[0-9a-f]+", re.IGNORECASE)
_BASE64 = re.compile(r"[A-Za-z0-9+/=_-]+")
_COMMON_HEX_ID = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})", re.IGNORECASE)
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _entropy(value: str) -> float:
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _looks_opaque(value: str, minimum_length: int) -> bool:
    candidate = value.strip().strip("'\"` ")
    if len(candidate) < minimum_length or _UUID.fullmatch(candidate):
        return False
    if not _BASE64.fullmatch(candidate):
        return False
    if _HEX.fullmatch(candidate):
        threshold = 3.0
    elif re.fullmatch(r"[A-Za-z0-9]+", candidate):
        threshold = 4.0
    else:
        threshold = 4.5
    return _entropy(candidate) >= threshold


def find_secret_category(content: str) -> str | None:
    """Return a fixed category only; never return a match, excerpt, or digest."""
    if _PRIVATE_KEY_BEGIN.search(content):
        return "private key"
    if _GITHUB_TOKEN.search(content):
        return "GitHub token"
    if _AWS_ACCESS_KEY_ID.search(content):
        return "AWS credential"

    for match in _ASSIGNMENT.finditer(content):
        name = match.group("name")
        if not _CREDENTIAL_NAME.search(name):
            continue
        value = match.group("value")
        if not _looks_opaque(value, minimum_length=12):
            continue
        if name.lower().startswith("aws_"):
            return "AWS credential"
        return "credential"

    for match in _OPAQUE_SPAN.finditer(content):
        candidate = match.group(0)
        if _COMMON_HEX_ID.fullmatch(candidate):
            continue
        if _looks_opaque(candidate, minimum_length=28):
            return "credential"
    return None


def scan_content(content: str) -> None:
    """Reject secret-like content without placing its value in the exception."""
    if not isinstance(content, str):
        raise ValueError("content must be text")
    category = find_secret_category(content)
    if category is not None:
        raise SecretAdmissionError(category)


def summarize_existing_content(contents) -> dict:
    """Return content-free category counts for a read-only preflight scan."""
    counts: Counter[str] = Counter()
    scanned = 0
    for content in contents:
        if not isinstance(content, str):
            raise ValueError("vault content must be text")
        scanned += 1
        category = find_secret_category(content)
        if category is not None:
            counts[category] += 1
    return {"scanned": scanned, "findings": dict(sorted(counts.items())),
            "safe": not counts}
