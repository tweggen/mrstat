"""Time-span (historical) and incremental synchronisation of MRs into the database."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from mrstat import db as dbm
from mrstat.analyze import Analyzer
from mrstat.timeutil import iso, parse_ts, utcnow

log = logging.getLogger(__name__)

STATE_KEY = "last_sync_until"
OVERLAP = timedelta(hours=1)


@dataclass
class SyncResult:
    since: datetime
    until: datetime
    analyzed: list[tuple[int, int]] = field(default_factory=list)
    failed: list[tuple[int, int, str]] = field(default_factory=list)


def _collect(an: Analyzer, since: datetime, until: datetime):
    gc = an.cfg.gitlab
    seen: set[tuple[int, int]] = set()
    sources = [("project", p) for p in gc.projects] + [("group", g) for g in gc.groups]
    if not sources:
        raise ValueError("nothing to sync: configure gitlab.projects and/or gitlab.groups")
    for kind, ref in sources:
        for mr in an.gl.list_mrs(**{kind: ref}, updated_after=since, state=gc.mr_state):
            ident = (mr["project_id"], mr["iid"])
            if ident in seen:
                continue
            if gc.target_branches and mr.get("target_branch") not in gc.target_branches:
                continue
            if gc.mr_state == "merged":
                merged = parse_ts(mr.get("merged_at"))
                if not merged or not (since <= merged <= until):
                    continue
            seen.add(ident)
            yield mr


def run_sync(an: Analyzer, since: datetime | None = None, until: datetime | None = None,
             incremental: bool = False) -> SyncResult:
    until = until or utcnow()
    if incremental:
        with an.db.connect() as conn:
            last = dbm.get_state(conn, STATE_KEY)
        if last:
            since = parse_ts(last) - OVERLAP
        elif since is None:
            raise ValueError("no previous sync recorded - pass an initial --since")
    if since is None:
        raise ValueError("--since is required")

    log.info("sync %s .. %s", iso(since), iso(until))
    res = SyncResult(since, until)
    mrs = list(_collect(an, since, until))
    log.info("%d merge requests to analyze", len(mrs))

    # 1) MR data, commits, issue keys, Jira trees (effort computed later, with full context)
    for n, mr in enumerate(mrs, 1):
        ident = (mr["project_id"], mr["iid"])
        try:
            an.analyze_mr(mr["project_id"], mr["iid"], mr=mr, fetch_context=False, compute=False)
            res.analyzed.append(ident)
            log.info("[%d/%d] %s!%s %s", n, len(mrs), *ident, mr.get("title", "")[:60])
        except Exception as exc:  # noqa: BLE001 - keep going, report at the end
            log.exception("failed to analyze %s!%s", *ident)
            res.failed.append((*ident, str(exc)))

    # 2) context commits: one window per project covering all its MRs' commits
    windows: dict[int, list[str]] = defaultdict(list)
    with an.db.connect() as conn:
        for pid, iid in res.analyzed:
            row = conn.execute(
                "SELECT MIN(c.authored_at) lo FROM commits c JOIN mr_commits m ON m.sha=c.sha "
                "WHERE m.project_id=? AND m.iid=?", (pid, iid)).fetchone()
            if row["lo"]:
                windows[pid].append(row["lo"])
    if an.cfg.effort.fetch_context:
        for pid, los in windows.items():
            try:
                an.fetch_project_commits(pid, parse_ts(min(los)) - timedelta(days=1),
                                         until + timedelta(days=1))
            except Exception:  # noqa: BLE001
                log.exception("failed to fetch context commits for project %s", pid)

    # 3) effort, now that every person's commit stream is as complete as we can get it
    for pid, iid in res.analyzed:
        an.compute_mr_effort(pid, iid)

    # advance the incremental marker only on a clean run so failed MRs are retried next time
    if not res.failed:
        with an.db.connect() as conn:
            prev = dbm.get_state(conn, STATE_KEY)
            if not prev or parse_ts(prev) < until:
                dbm.set_state(conn, STATE_KEY, iso(until))
    return res


def recompute_all(an: Analyzer) -> int:
    with an.db.connect() as conn:
        ids = [(r[0], r[1]) for r in conn.execute("SELECT project_id, iid FROM mrs")]
    for pid, iid in ids:
        an.compute_mr_effort(pid, iid)
    return len(ids)
