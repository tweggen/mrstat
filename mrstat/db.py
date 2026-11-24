"""SQLite storage. One file, safe for a web process plus a background sync thread (WAL)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from mrstat.timeutil import iso, norm, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY, path TEXT NOT NULL, web_url TEXT);

CREATE TABLE IF NOT EXISTS mrs(
  project_id INTEGER NOT NULL, iid INTEGER NOT NULL, gl_id INTEGER,
  title TEXT, description TEXT, author TEXT, state TEXT,
  source_branch TEXT, target_branch TEXT,
  created_at TEXT, merged_at TEXT, updated_at TEXT, web_url TEXT,
  gl_time_spent_s INTEGER, gl_time_estimate_s INTEGER, analyzed_at TEXT,
  PRIMARY KEY(project_id, iid));
CREATE INDEX IF NOT EXISTS mrs_merged ON mrs(merged_at);

CREATE TABLE IF NOT EXISTS commits(
  sha TEXT PRIMARY KEY, project_id INTEGER, author TEXT NOT NULL,
  author_name TEXT, author_email TEXT, authored_at TEXT NOT NULL, title TEXT);
CREATE INDEX IF NOT EXISTS commits_author_time ON commits(author, authored_at);

CREATE TABLE IF NOT EXISTS mr_commits(
  project_id INTEGER NOT NULL, iid INTEGER NOT NULL, sha TEXT NOT NULL,
  PRIMARY KEY(project_id, iid, sha));
CREATE INDEX IF NOT EXISTS mr_commits_sha ON mr_commits(sha);

CREATE TABLE IF NOT EXISTS mr_effort(
  project_id INTEGER NOT NULL, iid INTEGER NOT NULL, person TEXT NOT NULL,
  hours REAL NOT NULL, commits INTEGER NOT NULL,
  PRIMARY KEY(project_id, iid, person));

CREATE TABLE IF NOT EXISTS mr_issues(
  project_id INTEGER NOT NULL, iid INTEGER NOT NULL, issue_key TEXT NOT NULL, source TEXT,
  PRIMARY KEY(project_id, iid, issue_key));
CREATE INDEX IF NOT EXISTS mr_issues_key ON mr_issues(issue_key);

CREATE TABLE IF NOT EXISTS issues(
  key TEXT PRIMARY KEY, found INTEGER NOT NULL DEFAULT 1,
  summary TEXT, issue_type TEXT, status TEXT,
  timespent_s INTEGER, aggregate_timespent_s INTEGER,
  orig_estimate_s INTEGER, aggregate_orig_estimate_s INTEGER,
  story_points REAL, web_url TEXT, fetched_at TEXT);

-- normalised parent -> child edges; discovered_by = issue whose data produced the edge
CREATE TABLE IF NOT EXISTS issue_links(
  parent_key TEXT NOT NULL, child_key TEXT NOT NULL, relation TEXT NOT NULL,
  discovered_by TEXT NOT NULL,
  PRIMARY KEY(parent_key, child_key, relation, discovered_by));
CREATE INDEX IF NOT EXISTS issue_links_child ON issue_links(child_key);
CREATE INDEX IF NOT EXISTS issue_links_disc ON issue_links(discovered_by);

CREATE TABLE IF NOT EXISTS worklogs(
  id TEXT PRIMARY KEY, issue_key TEXT NOT NULL, person TEXT NOT NULL,
  started TEXT, seconds INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS worklogs_issue ON worklogs(issue_key);

CREATE TABLE IF NOT EXISTS sync_state(scope TEXT PRIMARY KEY, value TEXT);

-- one row per (MR, model, prompt version, input); the newest row is the current estimate
CREATE TABLE IF NOT EXISTS llm_estimates(
  project_id INTEGER NOT NULL, iid INTEGER NOT NULL, model TEXT NOT NULL,
  prompt_version TEXT NOT NULL, input_hash TEXT NOT NULL, created_at TEXT NOT NULL,
  hours_low REAL, hours_likely REAL, hours_high REAL, confidence TEXT,
  breakdown TEXT, complexity TEXT, rationale TEXT,
  strategy TEXT, input_chars INTEGER, files_included INTEGER, files_excluded INTEGER,
  prompt_tokens INTEGER, completion_tokens INTEGER, calls INTEGER,
  PRIMARY KEY(project_id, iid, model, prompt_version, input_hash));
CREATE INDEX IF NOT EXISTS llm_estimates_mr ON llm_estimates(project_id, iid, created_at);
"""

# columns added after the first release: (table, column, type)
MIGRATIONS = [("issues", "description", "TEXT")]


@dataclass(frozen=True)
class CommitRow:
    sha: str
    project_id: int
    author: str
    author_name: str | None
    author_email: str | None
    authored_at: str  # DB format
    title: str | None


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            for table, col, typ in MIGRATIONS:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=60)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


# -- writers -------------------------------------------------------------------------

def upsert_project(conn: sqlite3.Connection, p: dict) -> None:
    conn.execute(
        "INSERT INTO projects(id, path, web_url) VALUES(?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET path=excluded.path, web_url=excluded.web_url",
        (p["id"], p.get("path_with_namespace") or p.get("path") or str(p["id"]), p.get("web_url")),
    )


def upsert_mr(conn: sqlite3.Connection, project_id: int, mr: dict) -> None:
    ts = mr.get("time_stats") or {}
    author = mr.get("author") or {}
    conn.execute(
        """INSERT INTO mrs(project_id, iid, gl_id, title, description, author, state,
             source_branch, target_branch, created_at, merged_at, updated_at, web_url,
             gl_time_spent_s, gl_time_estimate_s)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(project_id, iid) DO UPDATE SET
             gl_id=excluded.gl_id, title=excluded.title, description=excluded.description,
             author=excluded.author, state=excluded.state, source_branch=excluded.source_branch,
             target_branch=excluded.target_branch, created_at=excluded.created_at,
             merged_at=excluded.merged_at, updated_at=excluded.updated_at,
             web_url=excluded.web_url, gl_time_spent_s=excluded.gl_time_spent_s,
             gl_time_estimate_s=excluded.gl_time_estimate_s""",
        (
            project_id, mr["iid"], mr.get("id"), mr.get("title"), mr.get("description"),
            author.get("username") or author.get("name"), mr.get("state"),
            mr.get("source_branch"), mr.get("target_branch"),
            norm(mr.get("created_at")), norm(mr.get("merged_at")), norm(mr.get("updated_at")),
            mr.get("web_url"), ts.get("total_time_spent"), ts.get("time_estimate"),
        ),
    )


def upsert_commits(conn: sqlite3.Connection, rows: Iterable[CommitRow]) -> None:
    conn.executemany(
        """INSERT INTO commits(sha, project_id, author, author_name, author_email, authored_at, title)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(sha) DO UPDATE SET author=excluded.author""",
        [(r.sha, r.project_id, r.author, r.author_name, r.author_email, r.authored_at, r.title) for r in rows],
    )


def set_mr_commits(conn, project_id: int, iid: int, shas: Iterable[str]) -> None:
    conn.execute("DELETE FROM mr_commits WHERE project_id=? AND iid=?", (project_id, iid))
    conn.executemany(
        "INSERT OR IGNORE INTO mr_commits(project_id, iid, sha) VALUES(?,?,?)",
        [(project_id, iid, s) for s in shas],
    )


def set_mr_issues(conn, project_id: int, iid: int, keys: dict[str, str]) -> None:
    conn.execute("DELETE FROM mr_issues WHERE project_id=? AND iid=?", (project_id, iid))
    conn.executemany(
        "INSERT INTO mr_issues(project_id, iid, issue_key, source) VALUES(?,?,?,?)",
        [(project_id, iid, k, src) for k, src in keys.items()],
    )


def set_mr_effort(conn, project_id: int, iid: int, per_person: dict[str, tuple[float, int]]) -> None:
    conn.execute("DELETE FROM mr_effort WHERE project_id=? AND iid=?", (project_id, iid))
    conn.executemany(
        "INSERT INTO mr_effort(project_id, iid, person, hours, commits) VALUES(?,?,?,?,?)",
        [(project_id, iid, p, h, n) for p, (h, n) in per_person.items()],
    )


def mark_analyzed(conn, project_id: int, iid: int) -> None:
    conn.execute(
        "UPDATE mrs SET analyzed_at=? WHERE project_id=? AND iid=?", (iso(utcnow()), project_id, iid)
    )


def upsert_issue(conn, row: dict) -> None:
    cols = ["key", "found", "summary", "description", "issue_type", "status", "timespent_s",
            "aggregate_timespent_s", "orig_estimate_s", "aggregate_orig_estimate_s",
            "story_points", "web_url", "fetched_at"]
    values = [row.get(c) for c in cols]
    conn.execute(
        f"INSERT INTO issues({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
        "ON CONFLICT(key) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in cols[1:]),
        values,
    )


def replace_issue_edges(conn, discovered_by: str, edges: Iterable[tuple[str, str, str]]) -> None:
    conn.execute("DELETE FROM issue_links WHERE discovered_by=?", (discovered_by,))
    conn.executemany(
        "INSERT OR IGNORE INTO issue_links(parent_key, child_key, relation, discovered_by) VALUES(?,?,?,?)",
        [(p, c, r, discovered_by) for p, c, r in edges],
    )


def replace_worklogs(conn, issue_key: str, rows: Iterable[tuple[str, str, str | None, int]]) -> None:
    conn.execute("DELETE FROM worklogs WHERE issue_key=?", (issue_key,))
    conn.executemany(
        "INSERT OR REPLACE INTO worklogs(id, issue_key, person, started, seconds) VALUES(?,?,?,?,?)",
        [(wid, issue_key, person, started, secs) for wid, person, started, secs in rows],
    )


def get_state(conn, scope: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE scope=?", (scope,)).fetchone()
    return row["value"] if row else None


def set_state(conn, scope: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state(scope, value) VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET value=excluded.value",
        (scope, value),
    )


def insert_llm_estimate(conn, row: dict) -> None:
    cols = list(row)
    conn.execute(
        f"INSERT OR REPLACE INTO llm_estimates({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
        [row[c] for c in cols],
    )


def latest_llm_estimate(conn, project_id: int, iid: int):
    return conn.execute(
        "SELECT * FROM llm_estimates WHERE project_id=? AND iid=? ORDER BY created_at DESC LIMIT 1",
        (project_id, iid)).fetchone()
