"""Read-only statistics queries over the database (used by `report` and the web UI)."""

from __future__ import annotations

from mrstat.db import Database


def _window(col: str, since: str | None, until: str | None, params: list) -> str:
    sql = ""
    if since:
        sql += f" AND {col} >= ?"
        params.append(since)
    if until:
        sql += f" AND {col} < ?"
        params.append(until)
    return sql


def mr_list(db: Database, since=None, until=None, project: str | None = None,
            person: str | None = None, limit: int = 500) -> list[dict]:
    params: list = []
    sql = """
      SELECT m.project_id, m.iid, m.title, m.web_url, m.state, m.merged_at, m.target_branch,
             p.path AS project_path,
             COALESCE((SELECT SUM(hours) FROM mr_effort e WHERE e.project_id=m.project_id AND e.iid=m.iid), 0) AS hours,
             (SELECT COUNT(*) FROM mr_commits c WHERE c.project_id=m.project_id AND c.iid=m.iid) AS commits,
             (SELECT GROUP_CONCAT(issue_key, ' ') FROM mr_issues i WHERE i.project_id=m.project_id AND i.iid=m.iid) AS issues,
             (SELECT GROUP_CONCAT(person, ', ') FROM mr_effort e WHERE e.project_id=m.project_id AND e.iid=m.iid) AS persons
      FROM mrs m LEFT JOIN projects p ON p.id=m.project_id WHERE 1=1"""
    sql += _window("COALESCE(m.merged_at, m.updated_at)", since, until, params)
    if project:
        sql += " AND p.path LIKE ?"
        params.append(f"%{project}%")
    if person:
        sql += " AND EXISTS (SELECT 1 FROM mr_effort e WHERE e.project_id=m.project_id AND e.iid=m.iid AND e.person=?)"
        params.append(person)
    sql += " ORDER BY COALESCE(m.merged_at, m.updated_at) DESC LIMIT ?"
    params.append(limit)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def persons(db: Database, since=None, until=None) -> list[dict]:
    """Per person: commit-based hours on MRs merged in the window, and Jira worklog hours
    started in the window."""
    p1: list = []
    w1 = _window("COALESCE(m.merged_at, m.updated_at)", since, until, p1)
    p2: list = []
    w2 = _window("w.started", since, until, p2)
    sql = f"""
      WITH mr AS (
        SELECT e.person, COUNT(*) AS mrs, SUM(e.commits) AS commits, SUM(e.hours) AS hours
        FROM mr_effort e JOIN mrs m ON m.project_id=e.project_id AND m.iid=e.iid
        WHERE 1=1 {w1} GROUP BY e.person),
      jira AS (
        SELECT w.person, SUM(w.seconds)/3600.0 AS jira_hours FROM worklogs w WHERE 1=1 {w2}
        GROUP BY w.person),
      everyone AS (SELECT person FROM mr UNION SELECT person FROM jira)
      SELECT everyone.person, COALESCE(mr.mrs,0) mrs, COALESCE(mr.commits,0) commits,
             COALESCE(mr.hours,0) hours, COALESCE(jira.jira_hours,0) jira_hours
      FROM everyone LEFT JOIN mr ON mr.person=everyone.person
                    LEFT JOIN jira ON jira.person=everyone.person
      ORDER BY hours DESC"""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, p1 + p2)]


def totals(db: Database) -> dict:
    with db.connect() as conn:
        r = conn.execute("""SELECT (SELECT COUNT(*) FROM mrs) mrs, (SELECT COUNT(*) FROM commits) commits,
                                   (SELECT COUNT(*) FROM issues WHERE found=1) issues,
                                   (SELECT value FROM sync_state WHERE scope='last_sync_until') last_sync""").fetchone()
        return dict(r)
