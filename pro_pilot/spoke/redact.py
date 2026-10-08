"""Redact common credentials from every projected tool result."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_REPLACEMENT = "[REDACTED:{kind}]"
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    ),
    (
        "bearer",
        re.compile(r"(?i)(?:authorization|bearer)\s*[:=]?\s*\S+"),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "aws_secret",
        re.compile(r"(?i)aws.{0,20}secret.{0,20}[:=]\s*\S{30,}"),
    ),
    (
        "kv_secret",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|client[_-]?secret)\b"
            r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|\S+)"
        ),
    ),
    (
        "conn_string",
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@]+@"),
    ),
    ("slack_token", re.compile(r"\b(?:xox[abprs]-[A-Za-z0-9-]+|xapp-[A-Za-z0-9-]+)\b")),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    ),
)
_PAN_PATTERN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def _luhn(value: str) -> bool:
    digits = [int(char) for char in value if char.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def redact_text(text: str) -> tuple[str, int]:
    """Redact one string and return the replacement count."""

    count = 0
    result = text
    for kind, pattern in _PATTERNS:
        result, matches = pattern.subn(_REPLACEMENT.format(kind=kind), result)
        count += matches

    def replace_pan(match: re.Match[str]) -> str:
        nonlocal count
        candidate = match.group(0)
        if not _luhn(candidate):
            return candidate
        count += 1
        return _REPLACEMENT.format(kind="pan")

    return _PAN_PATTERN.sub(replace_pan, result), count


def redact(value: Any) -> tuple[Any, int]:
    """Recursively redact strings in JSON-compatible data."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        count = 0
        for key, item in value.items():
            clean_item, item_count = redact(item)
            redacted[key] = clean_item
            count += item_count
        return redacted, count
    if isinstance(value, list):
        redacted_list = []
        count = 0
        for item in value:
            clean_item, item_count = redact(item)
            redacted_list.append(clean_item)
            count += item_count
        return redacted_list, count
    if isinstance(value, tuple):
        clean_items, count = redact(list(value))
        return tuple(clean_items), count
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return redact(list(value))
    return value, 0
