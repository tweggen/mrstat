"""Timestamp helpers. Everything stored in the DB is UTC in the form 2026-01-02T03:04:05Z
so that text comparison equals chronological comparison."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_COMPACT_OFFSET = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    s = _COMPACT_OFFSET.sub(r"\1:\2", s)  # Jira: +0100 -> +01:00
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm(value: str | None) -> str | None:
    """Normalise any API timestamp string to the DB format."""
    dt = parse_ts(value)
    return iso(dt) if dt else None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_date_arg(value: str) -> datetime:
    """CLI/web argument: YYYY-MM-DD (midnight UTC) or a full ISO timestamp."""
    dt = parse_ts(value)
    assert dt is not None
    return dt
