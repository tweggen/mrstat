"""Plain-text and JSON rendering for the CLI."""

from __future__ import annotations

import dataclasses
import json

from mrstat.compare import Comparison
from mrstat.db import Database


def mr_detail(db: Database, project_id: int, iid: int) -> dict:
    with db.connect() as conn:
        mr = conn.execute(
            "SELECT m.*, p.path AS project_path FROM mrs m LEFT JOIN projects p ON p.id=m.project_id "
            "WHERE m.project_id=? AND m.iid=?", (project_id, iid)).fetchone()
        if not mr:
            return {}
        effort = [dict(r) for r in conn.execute(
            "SELECT person, hours, commits FROM mr_effort WHERE project_id=? AND iid=? ORDER BY hours DESC",
            (project_id, iid))]
        issues = [dict(r) for r in conn.execute(
            "SELECT mi.issue_key, mi.source, i.summary, i.issue_type, i.status, i.found, i.web_url "
            "FROM mr_issues mi LEFT JOIN issues i ON i.key=mi.issue_key "
            "WHERE mi.project_id=? AND mi.iid=? ORDER BY mi.issue_key", (project_id, iid))]
        commits = [dict(r) for r in conn.execute(
            "SELECT c.sha, c.author, c.authored_at, c.title FROM commits c JOIN mr_commits m ON m.sha=c.sha "
            "WHERE m.project_id=? AND m.iid=? ORDER BY c.authored_at", (project_id, iid))]
    return {"mr": dict(mr), "effort": effort, "issues": issues, "commits": commits}


def _h(x: float) -> str:
    return f"{x:8.1f}"


def format_mr(detail: dict, cmp: Comparison | None, hours_per_day: float) -> str:
    mr = detail["mr"]
    out = [
        f"{mr.get('project_path') or mr['project_id']}!{mr['iid']}  {mr['title']}",
        f"  {mr.get('web_url') or ''}",
        f"  state={mr['state']}  {mr['source_branch']} -> {mr['target_branch']}  "
        f"created={mr['created_at']}  merged={mr['merged_at']}",
        f"  commits={len(detail['commits'])}"
        + (f"  GitLab time tracking: {mr['gl_time_spent_s'] / 3600:.1f} h" if mr.get("gl_time_spent_s") else ""),
        "",
        "  Effort estimated from commits:",
        f"    {'person':40} {'hours':>8} {'days':>8} {'commits':>8}",
    ]
    total = 0.0
    for e in detail["effort"]:
        total += e["hours"]
        out.append(f"    {e['person']:40} {_h(e['hours'])} {_h(e['hours'] / hours_per_day)} {e['commits']:8d}")
    out.append(f"    {'TOTAL':40} {_h(total)} {_h(total / hours_per_day)}")
    out.append("")
    if detail["issues"]:
        out.append("  Referenced Jira issues:")
        for i in detail["issues"]:
            state = "" if i["found"] in (1, None) else "  (not found)"
            out.append(f"    {i['issue_key']:14} [{i['source']}] {i['issue_type'] or ''} "
                       f"{i['status'] or ''}  {i['summary'] or ''}{state}")
    else:
        out.append("  No Jira issues referenced.")
    if cmp:
        out += ["", format_comparison(cmp, hours_per_day)]
    return "\n".join(out)


def format_comparison(cmp: Comparison, hours_per_day: float) -> str:
    out = ["  Comparison with Jira hierarchy:",
           f"    referenced: {', '.join(cmp.referenced) or '-'}",
           f"    roots:      {', '.join(cmp.roots) or '-'}",
           f"    closure:    {len(cmp.issues)} issues" + (f", missing: {', '.join(cmp.missing)}" if cmp.missing else ""),
           f"    MRs:        " + ", ".join(f"{m.get('project_path') or m['project_id']}!{m['iid']}" for m in cmp.mrs)]
    if cmp.other_mrs:
        out.append(f"    not included (use --expand): " + ", ".join(f"{p}!{i}" for p, i in cmp.other_mrs))
    out += ["", f"    {'person':40} {'MR h':>8} {'Jira h':>8}"]
    for p in cmp.persons:
        out.append(f"    {p:40} {_h(cmp.mr_by_person.get(p, 0))} {_h(cmp.jira_by_person.get(p, 0))}")
    out.append(f"    {'TOTAL':40} {_h(cmp.mr_hours)} {_h(cmp.jira_spent_h)}")
    out.append(f"    {'TOTAL person days':40} {_h(cmp.mr_hours / hours_per_day)} {_h(cmp.jira_spent_h / hours_per_day)}")
    out.append(f"    Jira original estimate: {cmp.jira_estimate_h:.1f} h   story points: {cmp.story_points:g}"
               + (f"   GitLab /spend: {cmp.gl_time_spent_h:.1f} h" if cmp.gl_time_spent_h else ""))
    if cmp.ratio is not None:
        out.append(f"    commit effort / Jira time spent = {cmp.ratio:.2f}")
    return "\n".join(out)


def format_issue_tree(cmp: Comparison) -> str:
    out = [f"    {'key':14} {'type':12} {'status':14} {'spent h':>8}  summary"]
    for i in cmp.issues:
        mark = "*" if i["root"] else ("+" if i["referenced"] else " ")
        out.append(f"  {mark} {i['key']:14} {(i['issue_type'] or '')[:12]:12} {(i['status'] or '')[:14]:14} "
                   f"{i['spent_h']:8.1f}  {i['summary'] or ''}")
    out.append("    (* root, + referenced by an MR)")
    return "\n".join(out)


def to_json(detail: dict | None, cmp: Comparison | None) -> str:
    payload = {}
    if detail:
        payload.update(detail)
    if cmp:
        c = dataclasses.asdict(cmp)
        c["ratio"] = cmp.ratio
        payload["comparison"] = c
    return json.dumps(payload, indent=2, default=str)
