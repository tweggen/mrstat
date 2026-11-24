"""Map the various identities of a person (git emails/names, Jira user names) to one key."""

from __future__ import annotations


class Identities:
    def __init__(self, mapping: dict[str, list[str]] | None = None):
        self._alias: dict[str, str] = {}
        for canonical, aliases in (mapping or {}).items():
            for alias in [canonical, *aliases]:
                self._alias[alias.strip().lower()] = canonical

    def resolve(self, *candidates: str | None) -> str:
        cands = [c.strip() for c in candidates if c and c.strip()]
        for c in cands:
            hit = self._alias.get(c.lower())
            if hit:
                return hit
        return cands[0].lower() if cands else "unknown"
