"""Command line interface.

  mrstat check                              test connectivity / credentials
  mrstat mr <ref> [iid]                     analyze one MR (ref: URL, group/proj!123, or project + iid)
  mrstat issue KEY                          Jira hierarchy of an issue vs. MRs in the DB
  mrstat sync --since 2026-01-01 [--until]  historical run over a time span
  mrstat sync --incremental                 continue from the last successful sync
  mrstat recompute                          recompute efforts from stored commits (after config changes)
  mrstat report --since ... [--csv FILE]    per-person / per-MR statistics from the DB
  mrstat serve [--sync-every 3600]          web UI (+ optional periodic incremental sync)
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from urllib.parse import urlparse

import requests

from mrstat import __version__
from mrstat.config import Config, ConfigError, load
from mrstat.db import Database
from mrstat.timeutil import iso, parse_date_arg

log = logging.getLogger("mrstat")

_MR_URL = re.compile(r"^/?(?P<path>.+?)/-/merge_requests/(?P<iid>\d+)")
_MR_BANG = re.compile(r"^(?P<path>[^!]+)!(?P<iid>\d+)$")


def parse_mr_ref(ref: str, iid: str | None) -> tuple[str, int]:
    if iid is not None:
        return ref, int(iid)
    if ref.startswith(("http://", "https://")):
        m = _MR_URL.match(urlparse(ref).path)
        if m:
            return m["path"], int(m["iid"])
    m = _MR_BANG.match(ref)
    if m:
        return m["path"], int(m["iid"])
    raise SystemExit(f"cannot parse MR reference {ref!r}: use a URL, group/project!IID or PROJECT IID")


def build(cfg: Config, *, need_gitlab=True, need_jira=False, no_jira=False):
    from mrstat.analyze import Analyzer
    from mrstat.gitlab_client import GitLabClient
    from mrstat.jira_client import JiraClient

    gl = GitLabClient(cfg.gitlab) if need_gitlab else None
    jira = None
    if not no_jira and (cfg.jira.url or need_jira):
        jira = JiraClient(cfg.jira)
    return Analyzer(cfg, Database(cfg.db_path), gl, jira)


# ------------------------------------------------------------------------- commands
def cmd_check(cfg: Config, args) -> int:
    ok = True
    if cfg.gitlab.url:
        from mrstat.gitlab_client import GitLabClient
        try:
            v = GitLabClient(cfg.gitlab).version()
            print(f"GitLab OK: {cfg.gitlab.url} version {v.get('version')}")
            for p in cfg.gitlab.projects:
                print(f"  project {p}: id {GitLabClient(cfg.gitlab).project(p)['id']}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"GitLab FAILED: {exc}")
    else:
        print("GitLab not configured")
    if cfg.jira.url:
        from mrstat.jira_client import JiraClient
        try:
            j = JiraClient(cfg.jira)
            info = j.server_info()
            me = j.myself()
            print(f"Jira OK: {cfg.jira.url} version {info.get('version')} as {me.get('name')}")
            keys = cfg.jira.project_keys or j.project_keys()
            print(f"  project keys used for matching ({len(keys)}): {', '.join(sorted(keys)[:30])}"
                  + (" ..." if len(keys) > 30 else ""))
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"Jira FAILED: {exc}")
    else:
        print("Jira not configured")
    print(f"Database: {cfg.db_path}")
    return 0 if ok else 1


def cmd_mr(cfg: Config, args) -> int:
    from mrstat import report
    from mrstat.compare import compare

    project, iid = parse_mr_ref(args.ref, args.iid)
    an = build(cfg, no_jira=args.no_jira)
    if args.no_context:
        cfg.effort.fetch_context = False
    pid, iid = an.analyze_mr(project, iid, with_jira=not args.no_jira)
    detail = report.mr_detail(an.db, pid, iid)
    cmp = compare(an.db, cfg, mrs=[(pid, iid)], expand=args.expand) if detail["issues"] else None
    if args.json:
        print(report.to_json(detail, cmp))
    else:
        print(report.format_mr(detail, cmp, cfg.effort.hours_per_day))
        if cmp and args.tree:
            print(report.format_issue_tree(cmp))
    return 0


def cmd_issue(cfg: Config, args) -> int:
    from mrstat import report
    from mrstat.compare import compare

    an = build(cfg, need_gitlab=False, need_jira=True)
    key = args.key.upper()
    an.fetch_issue_tree([key])
    cmp = compare(an.db, cfg, keys=[key], expand=False)
    if args.json:
        print(report.to_json(None, cmp))
    else:
        print(report.format_comparison(cmp, cfg.effort.hours_per_day))
        print(report.format_issue_tree(cmp))
    return 0


def cmd_sync(cfg: Config, args) -> int:
    from mrstat.sync import run_sync

    an = build(cfg, no_jira=args.no_jira)
    res = run_sync(
        an,
        since=parse_date_arg(args.since) if args.since else None,
        until=parse_date_arg(args.until) if args.until else None,
        incremental=args.incremental,
    )
    print(f"synced {iso(res.since)} .. {iso(res.until)}: {len(res.analyzed)} MRs, {len(res.failed)} failed")
    for pid, iid, err in res.failed:
        print(f"  FAILED {pid}!{iid}: {err}")
    return 1 if res.failed else 0


def cmd_recompute(cfg: Config, args) -> int:
    from mrstat.sync import recompute_all

    an = build(cfg, need_gitlab=False, no_jira=True)
    print(f"recomputed {recompute_all(an)} MRs")
    return 0


def cmd_report(cfg: Config, args) -> int:
    from mrstat import queries

    db = Database(cfg.db_path)
    since = iso(parse_date_arg(args.since)) if args.since else None
    until = iso(parse_date_arg(args.until)) if args.until else None
    hpd = cfg.effort.hours_per_day
    if args.by == "person":
        rows = queries.persons(db, since, until)
        header = ["person", "mrs", "commits", "hours", "days", "jira_hours"]
        data = [[r["person"], r["mrs"], r["commits"], round(r["hours"], 2), round(r["hours"] / hpd, 2),
                 round(r["jira_hours"], 2)] for r in rows]
    else:
        rows = queries.mr_list(db, since, until, limit=100000)
        header = ["project", "iid", "merged_at", "hours", "days", "issues", "title"]
        data = [[r["project_path"], r["iid"], r["merged_at"], round(r["hours"], 2), round(r["hours"] / hpd, 2),
                 r["issues"], r["title"]] for r in rows]
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(data)
        print(f"wrote {len(data)} rows to {args.csv}")
    else:
        print("\t".join(header))
        for d in data:
            print("\t".join(str(x) for x in d))
    return 0


def cmd_serve(cfg: Config, args) -> int:
    from mrstat.web.app import serve

    serve(cfg, host=args.host, port=args.port, sync_every=args.sync_every,
          initial_since=args.initial_since)
    return 0


# ------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mrstat", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", help="TOML config (default: $MRSTAT_CONFIG or ./mrstat.toml)")
    ap.add_argument("--db", help="override database path")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="test connectivity").set_defaults(func=cmd_check)

    p = sub.add_parser("mr", help="analyze a single merge request")
    p.add_argument("ref", help="MR URL, group/project!IID, or project path/id")
    p.add_argument("iid", nargs="?", help="MR iid if ref is a project")
    p.add_argument("--no-jira", action="store_true")
    p.add_argument("--no-context", action="store_true", help="only use the MR's own commits")
    p.add_argument("--expand", action=argparse.BooleanOptionalAction, default=True,
                   help="include all MRs referencing the same ticket hierarchy (default on)")
    p.add_argument("--tree", action="store_true", help="print the issue closure")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_mr)

    p = sub.add_parser("issue", help="Jira hierarchy of an issue vs. MRs in the DB")
    p.add_argument("key")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_issue)

    p = sub.add_parser("sync", help="analyze all MRs merged in a time span")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--incremental", action="store_true")
    p.add_argument("--no-jira", action="store_true")
    p.set_defaults(func=cmd_sync)

    sub.add_parser("recompute", help="recompute efforts from stored commits").set_defaults(func=cmd_recompute)

    p = sub.add_parser("report", help="statistics from the DB")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--by", choices=["mr", "person"], default="mr")
    p.add_argument("--csv", help="write CSV to this file")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("serve", help="run the web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--sync-every", type=int, default=0, help="seconds between incremental syncs (0=off)")
    p.add_argument("--initial-since", help="start date for the very first sync")
    p.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING - 10 * min(args.verbose, 2),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load(args.config)
        if args.db:
            cfg.db_path = args.db
        return args.func(cfg, args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except requests.HTTPError as exc:
        body = exc.response.text[:500] if exc.response is not None else ""
        print(f"HTTP error: {exc}\n{body}", file=sys.stderr)
        return 3
    except requests.RequestException as exc:
        print(f"connection error: {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
