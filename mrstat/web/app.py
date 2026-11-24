"""Small web UI + JSON API on top of the statistics database."""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mrstat import queries, report
from mrstat.compare import compare
from mrstat.config import Config
from mrstat.db import Database, latest_llm_estimate
from mrstat.timeutil import iso, parse_date_arg, utcnow

log = logging.getLogger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _date(value: str | None) -> str | None:
    return iso(parse_date_arg(value)) if value else None


def create_app(cfg: Config, analyzer_factory=None, sync_status: dict | None = None) -> FastAPI:
    """analyzer_factory: callable returning an Analyzer (with live GitLab/Jira clients);
    None disables on-demand analysis."""
    db = Database(cfg.db_path)
    app = FastAPI(title="mrstat")
    hpd = cfg.effort.hours_per_day
    status = sync_status if sync_status is not None else {}

    def render(request: Request, name: str, **ctx):
        return TEMPLATES.TemplateResponse(
            request, name, {"hpd": hpd, "can_analyze": analyzer_factory is not None,
                            "totals": queries.totals(db), "sync": status, **ctx})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, since: str | None = None, until: str | None = None,
              project: str | None = None, person: str | None = None):
        mrs = queries.mr_list(db, _date(since), _date(until), project or None, person or None)
        return render(request, "index.html", mrs=mrs, since=since or "", until=until or "",
                      project=project or "", person=person or "",
                      total_hours=sum(m["hours"] for m in mrs))

    @app.get("/persons", response_class=HTMLResponse)
    def persons(request: Request, since: str | None = None, until: str | None = None):
        rows = queries.persons(db, _date(since), _date(until))
        return render(request, "persons.html", rows=rows, since=since or "", until=until or "")

    @app.get("/mr/{project_id}/{iid}", response_class=HTMLResponse)
    def mr_page(request: Request, project_id: int, iid: int, expand: bool = True):
        detail = report.mr_detail(db, project_id, iid)
        if not detail:
            raise HTTPException(404, "MR not in database - analyze it first")
        cmp = compare(db, cfg, mrs=[(project_id, iid)], expand=expand) if detail["issues"] else None
        with db.connect() as conn:
            est = latest_llm_estimate(conn, project_id, iid)
        llm = None
        if est:
            llm = dict(est)
            llm["breakdown"] = json.loads(llm["breakdown"] or "{}")
            llm["complexity"] = json.loads(llm["complexity"] or "[]")
        return render(request, "mr.html", d=detail, cmp=cmp, expand=expand, llm=llm)

    @app.get("/issue/{key}", response_class=HTMLResponse)
    def issue_page(request: Request, key: str):
        cmp = compare(db, cfg, keys=[key.upper()], expand=False)
        return render(request, "issue.html", key=key.upper(), cmp=cmp)

    @app.post("/analyze")
    async def analyze(request: Request):
        if analyzer_factory is None:
            raise HTTPException(400, "analysis disabled")
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        from mrstat.cli import parse_mr_ref
        ref = form.get("ref", "").strip()
        if form.get("kind") == "issue":
            an = analyzer_factory()
            an.fetch_issue_tree([ref.upper()])
            return RedirectResponse(f"/issue/{ref.upper()}", status_code=303)
        try:
            project, iid = parse_mr_ref(ref, form.get("iid") or None)
        except SystemExit as exc:
            raise HTTPException(400, str(exc)) from None
        an = analyzer_factory()
        pid, iid = an.analyze_mr(project, iid)
        return RedirectResponse(f"/mr/{pid}/{iid}", status_code=303)

    # ---- JSON API
    @app.get("/api/mrs")
    def api_mrs(since: str | None = None, until: str | None = None, project: str | None = None,
                person: str | None = None, limit: int = 500):
        return queries.mr_list(db, _date(since), _date(until), project, person, limit)

    @app.get("/api/persons")
    def api_persons(since: str | None = None, until: str | None = None):
        return queries.persons(db, _date(since), _date(until))

    @app.get("/api/mr/{project_id}/{iid}")
    def api_mr(project_id: int, iid: int, expand: bool = True):
        detail = report.mr_detail(db, project_id, iid)
        if not detail:
            raise HTTPException(404)
        cmp = compare(db, cfg, mrs=[(project_id, iid)], expand=expand)
        return JSONResponse({**detail, "comparison": {**dataclasses.asdict(cmp), "ratio": cmp.ratio}})

    @app.get("/api/issue/{key}")
    def api_issue(key: str):
        cmp = compare(db, cfg, keys=[key.upper()], expand=False)
        return {**dataclasses.asdict(cmp), "ratio": cmp.ratio}

    @app.get("/api/sync")
    def api_sync():
        return status

    return app


def _sync_loop(factory, every: int, initial_since: str | None, status: dict,
               with_llm: bool = False) -> None:
    from mrstat.sync import run_sync

    since = parse_date_arg(initial_since) if initial_since else None
    while True:
        status.update(running=True, started=iso(utcnow()))
        try:
            from mrstat.progress import StatusProgress

            an = factory()
            res = run_sync(an, since=since, incremental=True, progress=StatusProgress(status))
            status.update(last_ok=iso(res.until), analyzed=len(res.analyzed), failed=len(res.failed),
                          error=None)
            if with_llm:
                from mrstat.estimate import Estimator
                from mrstat.llm_client import LLMClient
                est = Estimator(an.cfg, an.db, an.gl, LLMClient(an.cfg.llm))
                prog = StatusProgress(status)
                prog.phase("LLM estimates", len(res.analyzed))
                for pid, iid in res.analyzed:
                    try:
                        est.estimate(pid, iid)
                    except Exception:  # noqa: BLE001
                        log.exception("LLM estimate failed for %s!%s", pid, iid)
                    prog.advance(f"{pid}!{iid}")
                prog.done()
        except Exception as exc:  # noqa: BLE001
            log.exception("periodic sync failed")
            status.update(error=str(exc))
        status["running"] = False
        time.sleep(every)


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8080, sync_every: int = 0,
          initial_since: str | None = None, with_llm: bool = False) -> None:
    import uvicorn

    from mrstat.cli import build

    factory = (lambda: build(cfg)) if cfg.gitlab.url else None
    status: dict = {"enabled": bool(sync_every)}
    if sync_every and factory:
        threading.Thread(target=_sync_loop, args=(factory, sync_every, initial_since, status, with_llm),
                         daemon=True, name="mrstat-sync").start()
    uvicorn.run(create_app(cfg, factory, status), host=host, port=port)
