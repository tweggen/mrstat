"""Fetch data for MRs and Jira issues into the DB and compute per-MR effort."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from mrstat import db as dbm
from mrstat.config import Config
from mrstat.db import CommitRow, Database
from mrstat.effort import CommitEvent, EffortParams, attribute_hours
from mrstat.identities import Identities
from mrstat.keys import extract_keys
from mrstat.timeutil import iso, norm, parse_ts, utcnow

log = logging.getLogger(__name__)


@dataclass
class IssueNode:
    key: str
    children: set[str] = field(default_factory=set)
    parents: set[str] = field(default_factory=set)


class Analyzer:
    def __init__(self, cfg: Config, db: Database, gitlab=None, jira=None):
        self.cfg = cfg
        self.db = db
        self.gl = gitlab
        self.jira = jira
        self.ids = Identities(cfg.identities)
        self.params = EffortParams.from_config(cfg.effort)
        self._ignore = [re.compile(p, re.I) for p in cfg.effort.ignore_authors]
        self._projects: dict[str, dict] = {}
        self._allowed: set[str] | None = None
        self._allowed_loaded = False

    # ---------------------------------------------------------------- GitLab side
    def project(self, ref: str | int) -> dict:
        key = str(ref)
        if key not in self._projects:
            p = self.gl.project(ref)
            with self.db.connect() as conn:
                dbm.upsert_project(conn, p)
            self._projects[key] = self._projects[str(p["id"])] = p
        return self._projects[key]

    def _commit_row(self, project_id: int, c: dict) -> CommitRow | None:
        if self.cfg.effort.ignore_merge_commits and len(c.get("parent_ids") or []) > 1:
            return None
        name, email = c.get("author_name"), c.get("author_email")
        if any(rx.search(name or "") or rx.search(email or "") for rx in self._ignore):
            return None
        authored = norm(c.get("authored_date") or c.get("created_at"))
        if not authored:
            return None
        return CommitRow(
            sha=c["id"], project_id=project_id, author=self.ids.resolve(email, name),
            author_name=name, author_email=email, authored_at=authored, title=c.get("title"),
        )

    def analyze_mr(
        self,
        project_ref: str | int,
        iid: int,
        *,
        mr: dict | None = None,
        fetch_context: bool | None = None,
        with_jira: bool = True,
        compute: bool = True,
    ) -> tuple[int, int]:
        """Fetch one MR (+ its commits, linked issues) and compute its effort.
        Returns (project_id, iid)."""
        project = self.project(project_ref)
        pid = project["id"]
        mr = mr or self.gl.merge_request(pid, iid)
        raw_commits = list(self.gl.mr_commits(pid, iid))
        rows = [r for r in (self._commit_row(pid, c) for c in raw_commits) if r]

        with self.db.connect() as conn:
            dbm.upsert_mr(conn, pid, mr)
            dbm.upsert_commits(conn, rows)
            dbm.set_mr_commits(conn, pid, iid, [r.sha for r in rows])

        if fetch_context is None:
            fetch_context = self.cfg.effort.fetch_context
        if fetch_context and rows:
            first = min(parse_ts(r.authored_at) for r in rows)
            end = parse_ts(mr.get("merged_at")) or utcnow()
            self.fetch_project_commits(pid, first - timedelta(days=1), end + timedelta(days=1))

        keys = self.extract_mr_keys(mr, raw_commits)
        with self.db.connect() as conn:
            dbm.set_mr_issues(conn, pid, iid, keys)
        if with_jira and self.jira and keys:
            self.fetch_issue_tree(keys)

        if compute:
            self.compute_mr_effort(pid, iid)
        return pid, iid

    def fetch_project_commits(self, project_id: int, since, until) -> int:
        """Store all commits of a project in a window (context for the effort heuristic)."""
        log.info("fetching context commits of project %s %s..%s", project_id, iso(since), iso(until))
        rows = [r for r in (self._commit_row(project_id, c)
                            for c in self.gl.project_commits(project_id, since, until)) if r]
        with self.db.connect() as conn:
            dbm.upsert_commits(conn, rows)
        return len(rows)

    def compute_mr_effort(self, project_id: int, iid: int) -> dict[str, tuple[float, int]]:
        gap = timedelta(minutes=self.cfg.effort.max_gap_minutes)
        with self.db.connect() as conn:
            mr_rows = conn.execute(
                "SELECT c.sha, c.author, c.authored_at FROM commits c "
                "JOIN mr_commits m ON m.sha=c.sha WHERE m.project_id=? AND m.iid=?",
                (project_id, iid),
            ).fetchall()
            per_person: dict[str, tuple[float, int]] = {}
            if mr_rows:
                authors = sorted({r["author"] for r in mr_rows})
                times = [parse_ts(r["authored_at"]) for r in mr_rows]
                # pad by one day so per-day caps and the previous session are seen
                lo = iso(min(times) - gap - timedelta(days=1))
                hi = iso(max(times) + timedelta(days=1))
                ctx = conn.execute(
                    f"SELECT sha, author, authored_at FROM commits WHERE author IN "
                    f"({','.join('?' * len(authors))}) AND authored_at BETWEEN ? AND ?",
                    (*authors, lo, hi),
                ).fetchall()
                events = [CommitEvent(r["sha"], r["author"], parse_ts(r["authored_at"]))
                          for r in [*ctx, *mr_rows]]
                hours = attribute_hours(events, self.params)
                acc: dict[str, list] = defaultdict(lambda: [0.0, 0])
                for r in mr_rows:
                    acc[r["author"]][0] += hours.get(r["sha"], 0.0)
                    acc[r["author"]][1] += 1
                per_person = {p: (h, n) for p, (h, n) in acc.items()}
            dbm.set_mr_effort(conn, project_id, iid, per_person)
            dbm.mark_analyzed(conn, project_id, iid)
        return per_person

    # ---------------------------------------------------------------- issue keys
    def allowed_keys(self) -> set[str] | None:
        if not self._allowed_loaded:
            self._allowed_loaded = True
            if self.cfg.jira.project_keys:
                self._allowed = {k.upper() for k in self.cfg.jira.project_keys}
            elif self.jira and self.cfg.jira.auto_discover_projects:
                try:
                    self._allowed = set(self.jira.project_keys())
                except Exception as exc:  # noqa: BLE001 - degrade gracefully
                    log.warning("could not list Jira projects: %s", exc)
        return self._allowed

    def extract_mr_keys(self, mr: dict, raw_commits: list[dict]) -> dict[str, str]:
        allowed = self.allowed_keys()
        found: dict[str, str] = {}
        src = self.cfg.key_sources

        def add(text, source, ignore_case=False):
            for k in extract_keys(text, allowed, ignore_case=ignore_case):
                found.setdefault(k, source)

        if "title" in src:
            add(mr.get("title"), "title")
        if "source_branch" in src:
            add(mr.get("source_branch"), "branch", ignore_case=True)
        if "description" in src:
            add(mr.get("description"), "description")
        if "commits" in src:
            for c in raw_commits:
                add(c.get("message") or c.get("title"), "commit")
        return found

    # ---------------------------------------------------------------- Jira side
    def fetch_issue_tree(self, keys) -> set[str]:
        """Fetch referenced issues, optionally their ancestors, and all descendants."""
        jc = self.cfg.jira
        roots = set(keys)
        frontier = set(keys)
        for _ in range(jc.ascend_levels):
            parents: set[str] = set()
            for k in frontier:
                node = self.ensure_issue(k)
                if node:
                    parents |= node.parents
            frontier = parents - roots
            roots |= frontier
            if not frontier:
                break

        seen: set[str] = set()
        queue = [(k, 0) for k in sorted(roots)]
        while queue:
            key, depth = queue.pop()
            if key in seen:
                continue
            seen.add(key)
            node = self.ensure_issue(key)
            if node and depth < jc.max_depth:
                queue += [(c, depth + 1) for c in node.children if c not in seen]
        return seen

    def _fresh(self, conn, key: str) -> bool:
        row = conn.execute("SELECT fetched_at FROM issues WHERE key=?", (key,)).fetchone()
        if not row or not row["fetched_at"]:
            return False
        age = utcnow() - parse_ts(row["fetched_at"])
        return age < timedelta(hours=self.cfg.jira.cache_hours)

    def ensure_issue(self, key: str, force: bool = False) -> IssueNode | None:
        with self.db.connect() as conn:
            if not force and self._fresh(conn, key):
                found = conn.execute("SELECT found FROM issues WHERE key=?", (key,)).fetchone()["found"]
                if not found:
                    return None
                node = IssueNode(key)
                node.children = {r[0] for r in conn.execute(
                    "SELECT child_key FROM issue_links WHERE parent_key=?", (key,))}
                node.parents = {r[0] for r in conn.execute(
                    "SELECT parent_key FROM issue_links WHERE child_key=?", (key,))}
                return node
        return self._fetch_issue(key)

    def _fetch_issue(self, key: str) -> IssueNode | None:
        jc = self.cfg.jira
        fields = ["summary", "issuetype", "status", "parent", "subtasks", "issuelinks",
                  "timespent", "aggregatetimespent", "timeoriginalestimate",
                  "aggregatetimeoriginalestimate"]
        fields += [f for f in (jc.story_points_field, jc.epic_link_field) if f]
        log.debug("fetching Jira issue %s", key)
        data = self.jira.issue(key, fields)
        now = iso(utcnow())
        if data is None:
            with self.db.connect() as conn:
                dbm.upsert_issue(conn, {"key": key, "found": 0, "fetched_at": now})
                dbm.replace_issue_edges(conn, key, [])
            return None

        f = data.get("fields") or {}
        node = IssueNode(key)
        edges: list[tuple[str, str, str]] = []
        if jc.include_subtasks:
            for st in f.get("subtasks") or []:
                node.children.add(st["key"])
                edges.append((key, st["key"], "subtask"))
            if f.get("parent"):
                node.parents.add(f["parent"]["key"])
                edges.append((f["parent"]["key"], key, "subtask"))
        if jc.epic_link_field and f.get(jc.epic_link_field):
            epic = f[jc.epic_link_field]
            epic = epic.get("key") if isinstance(epic, dict) else epic
            node.parents.add(epic)
            edges.append((epic, key, "epic"))

        child_rel = {r.lower() for r in jc.child_relations}
        parent_rel = {r.lower() for r in jc.parent_relations}
        for link in f.get("issuelinks") or []:
            t = link.get("type") or {}
            if "outwardIssue" in link:
                phrase, other = (t.get("outward") or ""), link["outwardIssue"]["key"]
            elif "inwardIssue" in link:
                phrase, other = (t.get("inward") or ""), link["inwardIssue"]["key"]
            else:
                continue
            p = phrase.lower()
            if p in child_rel:
                node.children.add(other)
                edges.append((key, other, phrase))
            elif p in parent_rel:
                node.parents.add(other)
                edges.append((other, key, phrase))

        issue_type = (f.get("issuetype") or {}).get("name")
        if issue_type and issue_type.lower() == "epic" and jc.epic_children_jql:
            for child in self.jira.search_keys(jc.epic_children_jql.format(key=key)):
                if child != key:
                    node.children.add(child)
                    edges.append((key, child, "epic"))

        sp = f.get(jc.story_points_field) if jc.story_points_field else None
        row = {
            "key": key, "found": 1, "summary": f.get("summary"), "issue_type": issue_type,
            "status": (f.get("status") or {}).get("name"),
            "timespent_s": f.get("timespent"), "aggregate_timespent_s": f.get("aggregatetimespent"),
            "orig_estimate_s": f.get("timeoriginalestimate"),
            "aggregate_orig_estimate_s": f.get("aggregatetimeoriginalestimate"),
            "story_points": float(sp) if isinstance(sp, (int, float)) else None,
            "web_url": self.jira.browse_url(key), "fetched_at": now,
        }
        worklogs = None
        if jc.fetch_worklogs:
            worklogs = []
            for w in (self.jira.worklogs(key) if f.get("timespent") else []):
                a = w.get("author") or {}
                person = self.ids.resolve(a.get("emailAddress"), a.get("name"), a.get("key"),
                                          a.get("displayName"))
                worklogs.append((str(w["id"]), person, norm(w.get("started")),
                                 int(w.get("timeSpentSeconds") or 0)))
        with self.db.connect() as conn:
            dbm.upsert_issue(conn, row)
            dbm.replace_issue_edges(conn, key, edges)
            if worklogs is not None:
                dbm.replace_worklogs(conn, key, worklogs)
        return node
