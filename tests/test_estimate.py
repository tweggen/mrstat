import json

import pytest

from mrstat.analyze import Analyzer
from mrstat.config import Config, LLMConfig
from mrstat.db import Database
from mrstat.compare import compare
from mrstat.estimate import Estimator, classify_files
from mrstat.llm_client import LLMClient, LLMError, parse_json_object

from .fakes import FakeGitLab, FakeJira, FakeLLM, commit

GOOD = json.dumps({"hours_low": 2, "hours_likely": 4, "hours_high": 7,
                   "breakdown": {"implementation": 3, "tests": 1, "refactoring": 0, "config_infra": 0, "other": 0},
                   "complexity_factors": ["parser change"], "confidence": "medium", "rationale": "because"})


def diff(path, body, **kw):
    return {"old_path": path, "new_path": path, "diff": body, "new_file": False,
            "deleted_file": False, "renamed_file": False, **kw}


@pytest.fixture
def env(tmp_path):
    cfg = Config(db_path=str(tmp_path / "t.sqlite"))
    gl, jira = FakeGitLab(), FakeJira()
    gl.add_project(1, "grp/app")
    gl.add_mr(1, 1, [commit("a1", "Alice", "alice@corp", "2026-01-05T09:00:00Z")], title="APP-1 parser")
    gl.diffs[(1, 1)] = [
        diff("src/parser.py", "@@ -1,2 +1,3 @@\n-old\n+new\n+more\n"),
        diff("package-lock.json", "@@ -1 +1 @@\n-a\n+b\n"),
        diff("img/logo.png", ""),
        diff("src/gone.py", "@@ -1,2 +0,0 @@\n-x\n-y\n", deleted_file=True),
    ]
    jira.add("APP-1", spent=36000, estimate=28800)
    an = Analyzer(cfg, Database(cfg.db_path), gl, jira)
    an.analyze_mr("grp/app", 1)
    return cfg, an, gl


def test_classification():
    cfg = Config()
    files = {f.path: f for f in classify_files([
        diff("a/vendor/x.go", "@@\n+x\n"), diff("src/x.py", "@@\n+x\n-y\n"),
        diff("Foo.Designer.cs", "@@\n+x\n"), diff("big.c", "", too_large=True)], cfg)}
    assert files["a/vendor/x.go"].excluded
    assert files["src/x.py"].excluded is None and (files["src/x.py"].added, files["src/x.py"].deleted) == (1, 1)
    assert "too large" in files["big.c"].excluded


def test_prompt_is_blind_and_complete(env):
    cfg, an, gl = env
    prep = Estimator(cfg, an.db, gl).prepare(1, 1)
    text = "\n".join(m["content"] for m in prep.final_messages())
    assert "src/parser.py" in text and "+more" in text
    assert "description of APP-1" in text  # ticket context is included
    assert "package-lock.json" in text and "-a\n+b" not in text  # listed, not sent
    assert "-x\n-y" not in text  # deleted content not sent
    assert "28800" not in text and "36000" not in text  # no Jira estimate / booked time
    assert [f.path for f in prep.included] == ["src/parser.py"]


def test_estimate_store_cache_and_compare(env):
    cfg, an, gl = env
    llm = FakeLLM([GOOD])
    est = Estimator(cfg, an.db, gl, llm)
    row = est.estimate(1, 1)
    assert row["hours_likely"] == 4 and row["calls"] == 1
    assert llm.calls[0]["schema"]["required"]
    assert est.estimate(1, 1)["hours_likely"] == 4  # cached: no second call
    assert len(llm.calls) == 1
    cmp = compare(an.db, cfg, mrs=[(1, 1)])
    assert (cmp.llm_mrs, cmp.llm_likely_h, cmp.llm_high_h) == (1, 4, 7)


def test_invalid_answer_is_retried_once(env):
    cfg, an, gl = env
    llm = FakeLLM(["I think about 4 hours", "```json\n" + GOOD + "\n```"])
    assert Estimator(cfg, an.db, gl, llm).estimate(1, 1)["hours_high"] == 7
    assert len(llm.calls) == 2


def test_large_mr_summarize(env):
    cfg, an, gl = env
    gl.diffs[(1, 1)] = [diff(f"src/f{i}.py", "@@\n" + "+line\n" * 2000) for i in range(6)]
    cfg.llm.max_input_chars, cfg.llm.chunk_chars = 20_000, 30_000
    llm = FakeLLM(["summary 1", "summary 2", "summary 3", GOOD])  # 6 x 12k chars -> 3 chunks of 30k
    est = Estimator(cfg, an.db, gl, llm)
    prep = est.prepare(1, 1)
    assert prep.strategy == "summarize" and len(prep.chunks) == 3 and prep.planned_calls == 4
    row = est.estimate(1, 1)
    assert row["calls"] == 4 and "summary 3" in llm.calls[-1]["messages"][1]["content"]


def test_parse_json_object():
    assert parse_json_object('Sure:\n```json\n{"a": 1}\n```')["a"] == 1
    assert parse_json_object('x {"a": 2} y')["a"] == 2
    with pytest.raises(LLMError):
        parse_json_object("no json here")


def test_llm_client_request_shape(monkeypatch):
    cfg = LLMConfig(base_url="https://gw/v1/", model="m", api_key="k", api_key_header="api-key",
                    extra_query={"api-version": "2024"}, max_tokens_param="max_completion_tokens",
                    temperature="omit")
    c = LLMClient(cfg)
    seen = {}

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": GOOD}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 6}}

    def post(url, params=None, json=None, timeout=None):
        seen.update(url=url, params=params, body=json)
        return Resp()

    monkeypatch.setattr(c.s, "post", post)
    res = c.chat([{"role": "user", "content": "hi"}], json_schema={"type": "object"})
    assert seen["url"] == "https://gw/v1/chat/completions" and seen["params"] == {"api-version": "2024"}
    assert c.s.headers["api-key"] == "k" and "Authorization" not in c.s.headers
    assert seen["body"]["max_completion_tokens"] == 4000 and "temperature" not in seen["body"]
    assert seen["body"]["response_format"]["type"] == "json_schema"
    assert (res.prompt_tokens, res.completion_tokens) == (5, 6)


def test_web_shows_estimate(env):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from mrstat.web.app import create_app

    cfg, an, gl = env
    Estimator(cfg, an.db, gl, FakeLLM([GOOD])).estimate(1, 1)
    client = TestClient(create_app(cfg))
    page = client.get("/mr/1/1").text
    assert "LLM estimate of the final change" in page and "parser change" in page
    assert client.get("/").status_code == 200 and client.get("/issue/APP-1").status_code == 200
