"""Extract Jira issue keys from MR titles, descriptions, branch names and commit messages."""

from __future__ import annotations

import re

KEY_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9_]+-[1-9][0-9]*)(?![A-Za-z0-9])")


def extract_keys(text: str | None, allowed: set[str] | None = None, *, ignore_case: bool = False) -> list[str]:
    """Return keys in order of first appearance.

    With `allowed` (project keys), matching is case-insensitive (branch names are often
    lowercase) and keys of unknown projects (e.g. "UTF-8") are dropped. Without it, only
    upper-case keys match unless `ignore_case`.
    """
    if not text:
        return []
    if allowed is not None or ignore_case:
        text = text.upper()
    out: list[str] = []
    for m in KEY_RE.finditer(text):
        key = m.group(1)
        if allowed is not None and key.rsplit("-", 1)[0] not in allowed:
            continue
        if key not in out:
            out.append(key)
    return out
