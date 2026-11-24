"""`mrstat test` against a local stub server that mimics GitLab, Jira and an LLM gateway."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mrstat.config import Config
from mrstat.selftest import classify, format_results, run_all

JSON = "application/json"


def ok_routes():
    return {
        ("GET", "/api/v4/version"): (200, JSON, {"version": "17.3.1"}),
        ("GET", "/api/v4/user"): (200, JSON, {"username": "tw", "name": "T W"}),
        ("GET", "/api/v4/personal_access_tokens/self"): (200, JSON, {"scopes": ["read_api"], "expires_at": None}),
        ("GET", "/api/v4/projects/grp%2Fapp"): (200, JSON, {"id": 7, "path_with_namespace": "grp/app"}),
        ("GET", "/api/v4/projects/7/merge_requests"): (200, JSON, [{"iid": 1}]),
        ("GET", "/jira/rest/api/2/serverInfo"): (200, JSON, {"version": "9.12.0", "deploymentType": "Server"}),
        ("GET", "/jira/rest/api/2/myself"): (200, JSON, {"name": "tw", "displayName": "T W"}),
        ("GET", "/jira/rest/api/2/project"): (200, JSON, [{"key": "APP"}]),
        ("GET", "/jira/rest/api/2/field"): (200, JSON, [{"id": "customfield_10008", "name": "Epic Link"},
                                                       {"id": "customfield_10002", "name": "Story Points"}]),
        ("GET", "/jira/rest/api/2/issueLinkType"): (200, JSON, {"issueLinkTypes": [
            {"name": "Dependency", "outward": "requires", "inward": "is required by"},
            {"name": "Hierarchy", "outward": "is parent of", "inward": "is child of"},
            {"name": "Containment", "outward": "contains", "inward": "is contained in"}]}),
        ("GET", "/v1/models"): (200, JSON, {"data": [{"id": "test-model"}]}),
        ("POST", "/v1/chat/completions"): (200, JSON, {
            "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 4}}),
    }


@pytest.fixture
def server():
    routes = ok_routes()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self, method):
            if method == "POST":
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
            path = self.path.split("?")[0]
            status, ctype, body = routes.get((method, path), (404, JSON, {"message": "404 Not Found"}))
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", routes
    srv.shutdown()


def make_cfg(base):
    cfg = Config()
    cfg.gitlab.url, cfg.gitlab.token, cfg.gitlab.projects = base, "t", ["grp/app"]
    cfg.jira.url, cfg.jira.token = base + "/jira", "t"
    cfg.jira.epic_link_field = "customfield_10008"
    cfg.llm.base_url, cfg.llm.model = base + "/v1", "test-model"
    return cfg


def by_title(result):
    return {s.title: s for s in result.steps}


def test_all_ok(server):
    base, _ = server
    results = run_all(make_cfg(base), timeout=5)
    assert [r.status for r in results] == ["OK", "OK", "OK"], format_results(results, make_cfg(base))
    text = format_results(results, make_cfg(base))
    assert "GitLab 17.3.1" in text and "Jira 9.12.0" in text and "answered in" in text
    assert "story_points_field not set; candidates: customfield_10002" in text


def test_auth_failure_stops_service(server):
    base, routes = server
    routes[("GET", "/api/v4/user")] = (401, JSON, {"message": "401 Unauthorized"})
    gl = run_all(make_cfg(base), only=["gitlab"], timeout=5)[0]
    steps = by_title(gl)
    assert gl.status == "FAILED"
    assert "HTTP 401" in steps["authentication"].error and "MRSTAT_GITLAB_TOKEN" in steps["authentication"].hint
    assert "project grp/app" not in steps  # aborted after auth failure


def test_sso_login_page_is_recognised(server):
    base, routes = server
    routes[("GET", "/jira/rest/api/2/serverInfo")] = (
        200, "text/html; charset=utf-8", "<html><head><title>Corporate SSO Sign-In</title></head></html>")
    jira = run_all(make_cfg(base), only=["jira"], timeout=5)[0]
    err = by_title(jira)["server reachable"].error
    assert "text/html instead of JSON" in err and "Corporate SSO Sign-In" in err


def test_llm_rejects_response_format(server):
    base, routes = server
    routes[("POST", "/v1/chat/completions")] = (400, JSON, {"error": {"message": "response_format not supported"}})
    llm = run_all(make_cfg(base), only=["llm"], timeout=5)[0]
    step = by_title(llm)["test completion (a few tokens)"]
    assert llm.status == "FAILED" and "HTTP 400" in step.error and "response_format" in step.hint


def test_missing_link_phrase_warns(server):
    base, _ = server
    cfg = make_cfg(base)
    cfg.jira.child_relations = ["requires", "implements"]
    jira = run_all(cfg, only=["jira"], timeout=5)[0]
    assert jira.status == "OK with warnings"
    assert "implements" in by_title(jira)["issue link types"].detail


def test_connection_refused():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again: nothing listens there
    cfg = Config()
    cfg.gitlab.url, cfg.gitlab.token = f"http://127.0.0.1:{port}", "t"
    gl = run_all(cfg, only=["gitlab"], timeout=5)[0]
    assert by_title(gl)["server reachable"].error == "connection refused"


def test_not_configured_and_classify():
    results = run_all(Config(), timeout=1)
    assert [r.status for r in results] == ["not configured"] * 3
    import requests
    assert classify(requests.exceptions.ConnectionError(
        "Failed to resolve 'gitlab.corp' ([Errno -2] Name or service not known)"))[0].startswith("host name")
    assert "certificate" in classify(requests.exceptions.SSLError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in chain"))[0]
