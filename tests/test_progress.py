import io
from datetime import datetime, timezone

from mrstat.progress import ConsoleProgress, StatusProgress
from mrstat.sync import run_sync

from .test_pipeline import env  # noqa: F401  (fixture)


def _screen(raw: str) -> list[str]:
    """What a terminal shows: \\r returns to the line start, later text overwrites."""
    out = []
    for line in raw.split("\n"):
        out.append(line.split("\r")[-1].rstrip())
    return [l for l in out if l]


def test_lines_mode_sync(env):  # noqa: F811
    cfg, an, gl, jira = env
    gl.mrs[(1, 2)]["references"] = {"full": "grp/app!2"}
    buf = io.StringIO()
    run_sync(an, since=datetime(2026, 1, 1, tzinfo=timezone.utc),
             until=datetime(2026, 1, 9, tzinfo=timezone.utc), progress=ConsoleProgress("lines", buf))
    text = buf.getvalue()
    assert "[analyze] 1/3  33%" in text and "[analyze] 3/3 100%" in text
    assert "grp/app!2 MR 2" in text
    assert "[find MRs] 3" in text and "[context commits] 1/1" in text and "[compute effort] 3/3" in text


def test_bar_mode_and_failures(env):  # noqa: F811
    cfg, an, gl, jira = env
    gl.mr_commit_map[(1, 3)] = None  # makes analyzing MR 3 fail
    buf = io.StringIO()
    res = run_sync(an, since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                   until=datetime(2026, 1, 9, tzinfo=timezone.utc), progress=ConsoleProgress("bar", buf))
    assert res.failed and "\r" in buf.getvalue()
    screen = _screen(buf.getvalue())
    assert any(l.startswith("FAILED 1!3 APP-9 other") for l in screen)
    assert any(l.startswith("[analyze] 3/3 100%") for l in screen)  # final state of each phase stays
    assert screen[-1].startswith("[compute effort] 2/2 100%")


def test_status_progress():
    status = {}
    p = StatusProgress(status)
    p.phase("analyze", 4)
    p.advance("grp/app!1")
    assert status["progress"].startswith("[analyze] 1/4  25%") and "grp/app!1" in status["progress"]
    p.done()
    assert "progress" not in status
