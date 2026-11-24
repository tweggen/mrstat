"""Commit-timestamp based effort heuristic.

Git does not record working time, so we estimate it from each person's stream of commits
(across all MRs/projects we know of), similar to the well known "git-hours" approach:

* Sort a person's commits by *authored* date (survives rebases, unlike committed date).
* If the gap to that person's previous commit is <= max_gap, the gap is attributed to the
  commit (the person was working on it in between).
* Otherwise a new session starts and the commit gets `first_commit_minutes`
  (time spent before the first commit of a session).
* Per person and calendar day, the total is capped at `max_hours_per_day` (scaled down
  proportionally).

Because the stream is per person and global, interleaved work on several MRs is split
correctly: the time between a commit for MR A and the next commit for MR B goes to B.
Cherry-picks keep the authored date, so a backport commit has a 0 gap and costs ~nothing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable
from zoneinfo import ZoneInfo

from mrstat.config import EffortConfig


@dataclass(frozen=True)
class CommitEvent:
    sha: str
    author: str
    at: datetime  # timezone aware


@dataclass(frozen=True)
class EffortParams:
    max_gap_minutes: float = 120.0
    first_commit_minutes: float = 60.0
    max_hours_per_day: float = 10.0
    timezone: str = "UTC"

    @classmethod
    def from_config(cls, cfg: EffortConfig) -> "EffortParams":
        return cls(cfg.max_gap_minutes, cfg.first_commit_minutes, cfg.max_hours_per_day, cfg.timezone)


def attribute_hours(events: Iterable[CommitEvent], params: EffortParams) -> dict[str, float]:
    """Return estimated hours per commit sha."""
    tz = ZoneInfo(params.timezone)
    gap_s = params.max_gap_minutes * 60.0
    first_h = params.first_commit_minutes / 60.0

    by_author: dict[str, list[CommitEvent]] = defaultdict(list)
    seen: set[str] = set()
    for e in events:
        if e.sha in seen:
            continue
        seen.add(e.sha)
        by_author[e.author].append(e)

    result: dict[str, float] = {}
    for evs in by_author.values():
        evs.sort(key=lambda e: (e.at, e.sha))
        hours: dict[str, float] = {}
        prev: CommitEvent | None = None
        for e in evs:
            if prev is not None:
                delta = (e.at - prev.at).total_seconds()
                hours[e.sha] = delta / 3600.0 if delta <= gap_s else first_h
            else:
                hours[e.sha] = first_h
            prev = e

        if params.max_hours_per_day > 0:
            days: dict[object, list[str]] = defaultdict(list)
            for e in evs:
                days[e.at.astimezone(tz).date()].append(e.sha)
            for shas in days.values():
                total = sum(hours[s] for s in shas)
                if total > params.max_hours_per_day:
                    factor = params.max_hours_per_day / total
                    for s in shas:
                        hours[s] *= factor
        result.update(hours)
    return result
