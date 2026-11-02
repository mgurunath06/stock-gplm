"""Redaction and upload guards applied before any prompt leaves the process."""
from __future__ import annotations

import re

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("BEARER", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}")),
    ("API_KEY", re.compile(r"\b(?:sk|pk|or|rk)-[A-Za-z0-9_\-]{16,}\b")),
    ("SECRET_ASSIGNMENT", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|TOTP|PIN)[A-Z0-9_]*)\s*[=:]\s*\S+")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("LOCAL_PATH", re.compile(r"(?:/home|/Users|/mnt|[A-Za-z]:\\)[^\s'\"]*")),
    ("LONG_HEX", re.compile(r"\b[0-9a-fA-F]{40,}\b")),
)
# A line carrying an ISO date and at least four numbers looks like a candle or feature row.
_MARKET_ROW = re.compile(r"\d{4}-\d{2}-\d{2}(?:[^\n]*?-?\d+(?:\.\d+)?){4,}")


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Replace secrets, identities and local paths with typed placeholders; return counts."""
    counts: dict[str, int] = {}
    for name, pattern in _PATTERNS:
        text, hits = pattern.subn(f"[REDACTED_{name}]", text)
        if hits:
            counts[name] = hits
    return text, counts


def market_rows(text: str) -> int:
    """Count lines that look like tabular market data."""
    return sum(1 for line in text.splitlines() if _MARKET_ROW.search(line))
