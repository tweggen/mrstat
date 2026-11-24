"""Compare commit-based MR effort with Jira effort over the related ticket hierarchy.

MR <-> issue is n:m. Starting from a set of MRs (or an issue) we compute:

  referenced  = issue keys mentioned by the MRs
  start       = referenced + `ascend_levels` levels of parents
  roots       = minimal subset of `start`: drop keys that lie below another key of `start`
  closure     = roots + all descendants (subtasks, epic children, child link relations)
  MRs         = (with expand) every known MR referencing anything in the closure; repeat
                until stable - i.e. the connected cluster of MRs and tickets

and then compare sum(MR hours) with sum(own Jira time spent of each closure issue, counted
once), overall and per person.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from mrstat.config import Config
from mrstat.db import Database

MrId = tuple[int, int]


@dataclass
class Comparison:
    given_mrs: list[MrId]
    mrs: list[dict] = field(default_factory=list)
    other_mrs: list[MrId] = field(default_factory=list)  # reference closure, not included
    referenced: list[str] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)  # closure rows
    missing: list[str] = field(default_factory=list)  # not found / not fetched
    mr_hours: float = 0.0
    mr_by_person: dict[str, float] = field(default_factory=dict)
    jira_spent_h: float = 0.0
    jira_by_person: dict[str, float] = field(default_factory=dict)
    jira_estimate_h: float = 0.0
    story_points: float = 0.0
    gl_time_spent_h: float = 0.0  # GitLab /spend time tracking on the MRs

    @property
    def ratio(self) -> float | None:
        return self.mr_hours / self.jira_spent_h if self.jira_spent_h else None

    @property
    def persons(self) -> list[str]:
        return sorted(set(self.mr_by_person) | set(self.jira_by_person))


def _graph(conn):
    children: dict[str, set[str]] = defaultdict(set)
    parents: dict[str, set[str]] = defaultdict(set)
    for p, c in conn.execute("SELECT parent_key, child_key FROM issue_links"):
        if p != c:
            children[p].add(c)
            parents[c].add(p)
    return children, parents


def descendants(children, roots, max_depth: int) -> set[str]:
    out = set(roots)
    q = deque((r, 0) for r in roots)
    while q:
        k, d = q.popleft()
        if d >= max_depth:
            continue
        for c in children.get(k, ()):
            if c not in out:
                out.add(c)
                q.append((c, d + 1))
    return out


def ancestors(parents, keys, levels: int) -> set[str]:
    out = set(keys)
    frontier = set(keys)
    for _ in range(levels):
        frontier = {p for k in frontier for p in parents.get(k, ())} - out
        if not frontier:
            break
        out |= frontier
    return out


def minimal_roots(keys, children, max_depth: int) -> set[str]:
    keys = set(keys)
    covered: set[str] = set()
    for k in sorted(keys):
        if k in covered:
            continue
        covered |= (descendants(children, {k}, max_depth) - {k}) & keys
    return keys - covered


def _mr_keys(conn, mrs) -> set[str]:
    out: set[str] = set()
    for pid, iid in mrs:
        out |= {r[0] for r in conn.execute(
            "SELECT issue_key FROM mr_issues WHERE project_id=? AND iid=?", (pid, iid))}
    return out


def _referencing(conn, keys) -> set[MrId]:
    out: set[MrId] = set()
    keys = list(keys)
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        out |= {(r[0], r[1]) for r in conn.execute(
            f"SELECT DISTINCT project_id, iid FROM mr_issues WHERE issue_key IN ({','.join('?' * len(chunk))})",
            chunk)}
    return out


def compare(db: Database, cfg: Config, *, mrs=(), keys=(), expand: bool = True,
            max_rounds: int = 10) -> Comparison:
    """Start from MRs (expand=True -> whole cluster) or from issue keys (MRs referencing the
    closure are included once)."""
    jc = cfg.jira
    given = sorted(set(mrs))
    cmp = Comparison(given_mrs=given)
    with db.connect() as conn:
        children, parents = _graph(conn)
        cur = set(given)
        for _ in range(max_rounds):
            referenced = set(keys) | _mr_keys(conn, cur)
            start = ancestors(parents, referenced, jc.ascend_levels)
            roots = minimal_roots(start, children, jc.max_depth)
            closure = descendants(children, roots, jc.max_depth)
            new = _referencing(conn, closure) - cur
            if not new:
                break
            if expand or not given:
                cur |= new
                if not expand:
                    break
            else:
                cmp.other_mrs = sorted(new)
                break

        cmp.referenced, cmp.roots = sorted(referenced), sorted(roots)

        # -- MR side
        by_person: dict[str, float] = defaultdict(float)
        for pid, iid in sorted(cur):
            mr = conn.execute(
                "SELECT m.*, p.path AS project_path FROM mrs m LEFT JOIN projects p ON p.id=m.project_id "
                "WHERE m.project_id=? AND m.iid=?", (pid, iid)).fetchone()
            hours = 0.0
            for r in conn.execute(
                    "SELECT person, hours FROM mr_effort WHERE project_id=? AND iid=?", (pid, iid)):
                by_person[r["person"]] += r["hours"]
                hours += r["hours"]
            row = dict(mr) if mr else {"project_id": pid, "iid": iid, "title": "(not in DB)"}
            row["hours"] = hours
            cmp.mrs.append(row)
            cmp.mr_hours += hours
            cmp.gl_time_spent_h += ((mr["gl_time_spent_s"] or 0) / 3600) if mr else 0
        cmp.mr_by_person = dict(by_person)

        # -- Jira side
        jira_person: dict[str, float] = defaultdict(float)
        for key in sorted(closure):
            row = conn.execute("SELECT * FROM issues WHERE key=?", (key,)).fetchone()
            if not row or not row["found"]:
                cmp.missing.append(key)
                continue
            wl = conn.execute(
                "SELECT person, SUM(seconds) s FROM worklogs WHERE issue_key=? GROUP BY person",
                (key,)).fetchall()
            spent_s = sum(r["s"] for r in wl) if wl else (row["timespent_s"] or 0)
            for r in wl:
                jira_person[r["person"]] += r["s"] / 3600
            if not wl and row["timespent_s"]:
                jira_person["(no worklog detail)"] += row["timespent_s"] / 3600
            d = dict(row)
            d["spent_h"] = spent_s / 3600
            d["referenced"] = key in referenced
            d["root"] = key in roots
            cmp.issues.append(d)
            cmp.jira_spent_h += spent_s / 3600
            cmp.jira_estimate_h += (row["orig_estimate_s"] or 0) / 3600
            cmp.story_points += row["story_points"] or 0
        cmp.jira_by_person = dict(jira_person)
    return cmp
