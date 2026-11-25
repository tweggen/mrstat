"""End-to-end: fake GitLab + Jira -> analyzer -> DB -> comparison / sync / web."""

from datetime import datetime, timezone

import pytest

from mrstat.analyze import Analyzer
from mrstat.compare import compare, minimal_roots
from mrstat.config import Config
from mrstat.db import Database
from mrstat.sync import run_sync

from .fakes import FakeGitLab, FakeJira, commit

ALICE = ("Alice", "alice@corp")
BOB = ("Bob", "bob@corp")


@pytest.fixture
def env(tmp_path):
    cfg = Config(db_path=str(tmp_path / "t.sqlite"))
    cfg.gitlab.projects = ["grp/app"]
    cfg.identities = {"alice": ["alice@corp", "asmith"], "bob": ["bob@corp", "bjones"]}
    gl, jira = FakeGitLab(), FakeJira()
    gl.add_project(1, "grp/app")

    # MR 1 (APP-1 story): alice works 9:00-10:30 in 3 commits, bob one commit.
    gl.add_mr(1, 1, [
        commit("a1", *ALICE, "2026-01-05T09:00:00Z", "APP-1 start"),
        commit("a2", *ALICE, "2026-01-05T09:45:00Z"),
        commit("a3", *ALICE, "2026-01-05T10:30:00Z"),
        commit("b1", *BOB, "2026-01-05T14:00:00Z"),
        commit("m1", *BOB, "2026-01-05T15:00:00Z", "Merge branch main", parents=2),
    ], title="APP-1 feature", merged_at="2026-01-06T10:00:00Z")
    # MR 2 references the subtask APP-3; alice continues 20 min after a3.
    gl.add_mr(1, 2, [commit("a4", *ALICE, "2026-01-05T10:50:00Z")],
              source_branch="feature/app-3-sub", merged_at="2026-01-07T10:00:00Z")
    # MR 3 unrelated ticket
    gl.add_mr(1, 3, [commit("b2", *BOB, "2026-01-08T09:00:00Z")], title="APP-9 other",
              merged_at="2026-01-08T12:00:00Z")

    jira.add("APP-1", subtasks=["APP-3"], links=[("requires", "APP-2", "outward")], spent=3600 * 4,
             estimate=3600 * 8, worklogs=[("asmith", "", 3600 * 3), ("bjones", "", 3600)])
    jira.add("APP-2", spent=3600 * 2, worklogs=[("bjones", "bob@corp", 3600 * 2)])
    jira.add("APP-3", parent="APP-1", spent=1800, worklogs=[("asmith", "", 1800)])
    jira.add("APP-9", spent=3600, worklogs=[("bjones", "", 3600)])
    an = Analyzer(cfg, Database(cfg.db_path), gl, jira)
    return cfg, an, gl, jira


def test_single_mr_effort_and_comparison(env):
    cfg, an, gl, jira = env
    pid, iid = an.analyze_mr("grp/app", 1)
    with an.db.connect() as conn:
        eff = {r["person"]: (r["hours"], r["commits"]) for r in conn.execute("SELECT * FROM mr_effort")}
        keys = {r["issue_key"] for r in conn.execute("SELECT issue_key FROM mr_issues")}
    assert eff["alice"] == pytest.approx((1 + 0.75 + 0.75, 3))
    assert eff["bob"] == pytest.approx((1.0, 1))  # merge commit ignored
    assert keys == {"APP-1"}

    cmp = compare(an.db, cfg, mrs=[(pid, iid)], expand=False)
    assert cmp.roots == ["APP-1"]
    assert {i["key"] for i in cmp.issues} == {"APP-1", "APP-2", "APP-3"}
    assert cmp.jira_spent_h == pytest.approx(4 + 2 + 0.5)
    assert cmp.jira_by_person == pytest.approx({"alice": 3.5, "bob": 3.0})
    assert cmp.jira_estimate_h == pytest.approx(8)


def test_cluster_expansion_and_global_author_stream(env):
    cfg, an, gl, jira = env
    an.analyze_mr("grp/app", 1)
    an.analyze_mr("grp/app", 2)
    an.compute_mr_effort(1, 1)
    with an.db.connect() as conn:
        a4 = conn.execute("SELECT hours FROM mr_effort WHERE iid=2").fetchone()["hours"]
    assert a4 == pytest.approx(20 / 60)  # continues alice's session from MR 1

    cmp = compare(an.db, cfg, mrs=[(1, 1)], expand=True)
    assert [m["iid"] for m in cmp.mrs] == [1, 2]
    assert cmp.mr_hours == pytest.approx(2.5 + 1 + 20 / 60)

    cmp = compare(an.db, cfg, mrs=[(1, 1)], expand=False)
    assert cmp.other_mrs == [(1, 2)]


def test_issue_start_minimal_roots(env):
    cfg, an, gl, jira = env
    an.analyze_mr("grp/app", 2)  # references APP-3 only
    an.fetch_issue_tree(["APP-1"])
    cmp = compare(an.db, cfg, keys=["APP-1"], expand=False)
    assert [m["iid"] for m in cmp.mrs] == [2]
    assert minimal_roots({"APP-1", "APP-3", "APP-2"}, {"APP-1": {"APP-2", "APP-3"}}, 5) == {"APP-1"}


def test_ascend_levels(env):
    cfg, an, gl, jira = env
    cfg.jira.ascend_levels = 1
    pid, iid = an.analyze_mr("grp/app", 2)
    cmp = compare(an.db, cfg, mrs=[(pid, iid)], expand=False)
    assert cmp.roots == ["APP-1"]


def test_jira_cache(env):
    cfg, an, gl, jira = env
    an.analyze_mr("grp/app", 1)
    n = jira.fetches
    an.analyze_mr("grp/app", 1)
    assert jira.fetches == n


def test_sync_historical_then_incremental(env):
    cfg, an, gl, jira = env
    res = run_sync(an, since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                   until=datetime(2026, 1, 7, 12, tzinfo=timezone.utc))
    assert sorted(res.analyzed) == [(1, 1), (1, 2)]
    res = run_sync(an, until=datetime(2026, 1, 9, tzinfo=timezone.utc), incremental=True)
    assert res.analyzed == [(1, 3)]


def test_web_pages(env):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from mrstat.web.app import create_app

    cfg, an, gl, jira = env
    an.analyze_mr("grp/app", 1)
    an.analyze_mr("grp/app", 2)
    client = TestClient(create_app(cfg, lambda: an))
    for url in ["/", "/persons", "/mr/1/1", "/mr/1/1?expand=false", "/issue/APP-1",
                "/api/mrs", "/api/mr/1/1", "/api/issue/APP-1", "/api/persons"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:500])
    assert "APP-2" in client.get("/mr/1/1").text
    r = client.post("/analyze", content="ref=grp/app!3&kind=mr",
                    headers={"content-type": "application/x-www-form-urlencoded"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/mr/1/3"


def test_web_list_limit(env):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from mrstat.web.app import create_app

    cfg, an, gl, jira = env
    for iid in (1, 2, 3):
        an.analyze_mr("grp/app", iid)
    client = TestClient(create_app(cfg, lambda: an))
    assert "most recent merge requests only" not in client.get("/").text
    page = client.get("/?limit=2").text
    assert "Showing the 2 most recent merge requests only" in page
    assert "grp/app!3" in page and "grp/app!1<" not in page
    assert len(client.get("/api/mrs?limit=2").json()) == 2
