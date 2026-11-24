"""`mrstat test`: check the configuration against GitLab, Jira and the LLM endpoint and
explain failures (DNS, refused connection, timeout, certificate, proxy, SSO login page
instead of the API, 401/403/404, ...)."""

from __future__ import annotations

import dataclasses
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import requests

from mrstat.config import Config, ConfigError
from mrstat.http import without_retries
from mrstat.llm_client import LLMError

SERVICES = ("gitlab", "jira", "llm")


@dataclass
class Step:
    status: str  # ok | warn | fail | skip
    title: str
    detail: str = ""
    error: str = ""  # category, e.g. "authentication failed (HTTP 401)"
    hint: str = ""


@dataclass
class ServiceResult:
    name: str
    url: str
    steps: list[Step] = field(default_factory=list)

    @property
    def status(self) -> str:
        states = {s.status for s in self.steps}
        if "fail" in states:
            return "FAILED"
        if states <= {"skip"}:
            return "not configured"
        return "OK with warnings" if "warn" in states else "OK"


class NotJSON(Exception):
    """The server answered, but not with the expected JSON (typically an SSO login page)."""

    def __init__(self, resp: requests.Response):
        self.resp = resp
        ctype = resp.headers.get("Content-Type", "?")
        m = re.search(r"<title[^>]*>(.*?)</title>", resp.text[:5000], re.I | re.S)
        title = f', page title "{m.group(1).strip()[:80]}"' if m else ""
        where = f", after redirect to {resp.url}" if resp.history else ""
        super().__init__(f"got {ctype.split(';')[0]} instead of JSON{title}{where}")


class Abort(Exception):
    """Stop testing this service (later steps would only repeat the same error)."""


# ----------------------------------------------------------------------- classification
_DNS = ("name or service not known", "getaddrinfo failed", "nodename nor servname",
        "failed to resolve", "temporary failure in name resolution", "no address associated")
_REFUSED = ("connection refused", "actively refused", "errno 111", "winerror 10061")
_RESET = ("connection reset", "connection aborted", "remote end closed", "remotedisconnected",
          "forcibly closed")


def _status_error(status: int, body: str, auth_hint: str) -> tuple[str, str]:
    snippet = re.sub(r"\s+", " ", body or "")[:200]
    detail = f" - server says: {snippet}" if snippet else ""
    if status == 401:
        return (f"authentication failed (HTTP 401){detail}", auth_hint)
    if status == 403:
        return (f"access denied (HTTP 403){detail}",
                "The credentials are accepted but lack permission (token scope, project/user "
                "permissions). Jira also answers 403 when a CAPTCHA is required after failed "
                "logins - log in once in the browser.")
    if status == 404:
        return (f"not found (HTTP 404){detail}",
                "Check the base URL: no /api/v4 or /rest/api/2 suffix, but include a context path "
                "if the server has one (e.g. https://host/jira). For projects: check the path.")
    if status == 407:
        return ("proxy authentication required (HTTP 407)",
                "Your proxy needs credentials, or the internal host should bypass it: "
                "set NO_PROXY=<internal domain>.")
    if status == 429:
        return ("rate limited (HTTP 429)", "Too many requests - wait and retry.")
    if status in (502, 503, 504):
        return (f"server or gateway unavailable (HTTP {status}){detail}",
                "The service or a proxy/load balancer in front of it is down or overloaded.")
    if status >= 500:
        return (f"server error (HTTP {status}){detail}", "Server-side problem; check its logs.")
    return (f"HTTP {status}{detail}", "")


def classify(exc: BaseException, auth_hint: str = "") -> tuple[str, str]:
    """Return (error category with detail, hint)."""
    E = requests.exceptions
    text = str(exc)
    low = text.lower()
    if isinstance(exc, ConfigError):
        return (f"configuration error: {text}", "Fix the setting in mrstat.toml or the environment.")
    if isinstance(exc, NotJSON):
        return (f"unexpected response: {text}",
                "The URL reaches a web page (often an SSO/login page or the web UI) instead of the "
                "REST API. Check the base URL and whether API access bypasses SSO.")
    if isinstance(exc, LLMError) and exc.status:
        cat, hint = _status_error(exc.status, exc.body, auth_hint)
        if exc.status == 400:
            cat = f"request rejected (HTTP 400): {exc.body[:300]}"
            hint = ("Check llm.model (exact name the server expects). If the message mentions "
                    "response_format, set llm.response_format = \"json_object\" or \"none\"; if it "
                    "mentions max_tokens or temperature, try max_tokens_param = "
                    "\"max_completion_tokens\" or temperature = \"omit\".")
        return cat, hint
    if isinstance(exc, LLMError):
        return (f"LLM answer unusable: {text}",
                "The endpoint works but the reply was empty or not JSON. Try "
                "llm.response_format = \"json_object\" or \"none\", or a larger llm.max_tokens.")
    if isinstance(exc, E.HTTPError) and exc.response is not None:
        return _status_error(exc.response.status_code, exc.response.text, auth_hint)
    if isinstance(exc, E.SSLError):
        if "certificate verify failed" in low or "self signed" in low or "self-signed" in low:
            return ("TLS certificate not trusted",
                    "The server uses a certificate from an internal CA. Set verify_ssl = "
                    "\"<path to CA .pem>\" in this section, or `pip install pip-system-certs` to use "
                    "the Windows certificate store.")
        if "hostname" in low and "match" in low:
            return ("TLS certificate does not match the host name",
                    "Use the host name the certificate was issued for (as in the browser).")
        return (f"TLS/SSL error: {text[:200]}", "Check verify_ssl and the URL scheme (https).")
    if isinstance(exc, E.ProxyError):
        return (f"proxy error: {text[:200]}",
                "Check HTTPS_PROXY / HTTP_PROXY. For internal hosts that are reached directly, set "
                "NO_PROXY=<internal domain>.")
    if isinstance(exc, (E.ConnectTimeout,)):
        return ("connection timed out",
                "Host not reachable from this machine: VPN/corporate network, firewall, or proxy "
                "needed?")
    if isinstance(exc, (E.ReadTimeout, E.Timeout)):
        return ("no response in time (read timeout)",
                "The server accepted the connection but did not answer in time; increase the "
                "timeout or check server load.")
    if isinstance(exc, (E.MissingSchema, E.InvalidSchema, E.InvalidURL, E.URLRequired)):
        return (f"invalid URL: {text[:200]}", "Use a full URL like https://host or https://host/jira.")
    if isinstance(exc, E.JSONDecodeError):
        return ("response is not valid JSON",
                "The URL probably points to a web page, not the API. Check the base URL.")
    if isinstance(exc, E.ConnectionError):
        if any(s in low for s in _DNS):
            return ("host name cannot be resolved (DNS)",
                    "Typo in the host name, or this machine is not in the corporate network/VPN.")
        if any(s in low for s in _REFUSED):
            return ("connection refused",
                    "The host is reachable but nothing listens on that port: check port and "
                    "http vs. https.")
        if any(s in low for s in _RESET):
            return ("connection reset by the server or a proxy",
                    "Often a proxy or firewall dropping the connection, or http/https mixed up.")
        if "timed out" in low:
            return ("connection timed out", "Host not reachable: VPN, firewall or proxy?")
        return (f"connection error: {text[:200]}", "")
    return (f"{type(exc).__name__}: {text[:200]}", "")


# ----------------------------------------------------------------------- runner helpers
class _Runner:
    def __init__(self, result: ServiceResult, auth_hint: str):
        self.r = result
        self.auth_hint = auth_hint

    def step(self, title: str, fn: Callable[[], str | tuple | None], *,
             fatal: bool = True, warn_only: bool = False) -> bool:
        """fn returns a detail string, or (status, detail[, hint]) to override the status."""
        try:
            out = fn()
        except Abort:
            raise
        except Exception as exc:  # noqa: BLE001 - every error is reported
            cat, hint = classify(exc, self.auth_hint)
            self.r.steps.append(Step("warn" if warn_only else "fail", title, error=cat, hint=hint))
            if fatal and not warn_only:
                raise Abort() from exc
            return False
        if not isinstance(out, tuple):
            out = ("ok", out)
        status, detail, hint = (*out, "")[:3]
        self.r.steps.append(Step(status, title, detail or "", hint=hint))
        return status != "fail"

    def add(self, status: str, title: str, detail: str = "", hint: str = "") -> None:
        self.r.steps.append(Step(status, title, detail, hint=hint))


def _get(session: requests.Session, url: str, timeout: float, params=None):
    r = session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    if "json" not in r.headers.get("Content-Type", "").lower():
        raise NotJSON(r)
    return r.json()


# ----------------------------------------------------------------------- GitLab
def test_gitlab(cfg: Config, timeout: float) -> ServiceResult:
    from mrstat.gitlab_client import GitLabClient

    gc = cfg.gitlab
    res = ServiceResult("GitLab", gc.url)
    run = _Runner(res, "Token missing, wrong, revoked or expired: create a personal access token "
                       "with scope read_api and set MRSTAT_GITLAB_TOKEN.")
    if not gc.url:
        run.add("skip", "not configured", hint="set gitlab.url in mrstat.toml or MRSTAT_GITLAB_URL")
        return res
    if not gc.token:
        run.add("warn", "no token configured", hint="set MRSTAT_GITLAB_TOKEN (scope read_api)")
    try:
        gl = GitLabClient(gc)
        without_retries(gl.s)
        api = gl.api

        def version():
            v = _get(gl.s, api + "/version", timeout)
            return f"GitLab {v.get('version', '?')}"

        def user():
            u = _get(gl.s, api + "/user", timeout)
            return f"as {u.get('username')} ({u.get('name')})"

        def token():
            r = gl.s.get(api + "/personal_access_tokens/self", timeout=timeout)
            if r.status_code in (401, 403, 404):
                return ("skip", "token details not available (older GitLab or non-personal token)")
            r.raise_for_status()
            t = r.json()
            scopes = t.get("scopes") or []
            detail = f"scopes: {', '.join(scopes) or '-'}"
            if t.get("expires_at"):
                days = (datetime.fromisoformat(t["expires_at"]).replace(tzinfo=timezone.utc)
                        - datetime.now(timezone.utc)).days
                detail += f", expires {t['expires_at']} ({days} days)"
                if days < 14:
                    return ("warn", detail + " - renew soon")
            if not {"api", "read_api"} & set(scopes):
                return ("warn", detail + " - needs read_api")
            return detail

        run.step("server reachable", version)
        run.step("authentication", user)
        run.step("token", token, fatal=False, warn_only=True)

        for ref in gc.projects:
            def project(ref=ref):
                p = gl.project(ref)
                mrs = _get(gl.s, f"{api}/projects/{p['id']}/merge_requests", timeout,
                           {"per_page": 1, "scope": "all"})
                return f"id {p['id']}, merge requests readable ({'some' if mrs else 'none yet'})"
            run.step(f"project {ref}", project, fatal=False)
        for ref in gc.groups:
            def group(ref=ref):
                g = _get(gl.s, f"{api}/groups/{gl._ref(ref)}", timeout)
                return f"id {g['id']} ({g.get('full_path')})"
            run.step(f"group {ref}", group, fatal=False)
        if not gc.projects and not gc.groups:
            run.add("warn", "no projects or groups configured",
                    hint="needed for `sync` (single MRs work without)")
    except Abort:
        pass
    except ConfigError as exc:
        res.steps.append(Step("fail", "configuration", error=str(exc)))
    return res


# ----------------------------------------------------------------------- Jira
def test_jira(cfg: Config, timeout: float) -> ServiceResult:
    from mrstat.jira_client import JiraClient

    jc = cfg.jira
    res = ServiceResult("Jira", jc.url)
    run = _Runner(res, "Token or password wrong/expired: create a personal access token (Profile -> "
                       "Personal Access Tokens) and set MRSTAT_JIRA_TOKEN, or use "
                       "MRSTAT_JIRA_USERNAME + MRSTAT_JIRA_PASSWORD. A PAT needs Jira >= 8.14.")
    if not jc.url:
        run.add("skip", "not configured", hint="set jira.url in mrstat.toml or MRSTAT_JIRA_URL")
        return res
    if not (jc.token or jc.username):
        run.add("warn", "no credentials configured",
                hint="set MRSTAT_JIRA_TOKEN, or MRSTAT_JIRA_USERNAME + MRSTAT_JIRA_PASSWORD")
    try:
        j = JiraClient(jc)
        without_retries(j.s)
        api = j.api
        fields: dict[str, str] = {}

        def server():
            i = _get(j.s, api + "/serverInfo", timeout)
            return f"Jira {i.get('version', '?')} ({i.get('deploymentType', '?')})"

        def myself():
            m = _get(j.s, api + "/myself", timeout)
            return f"as {m.get('name')} ({m.get('displayName')})"

        def projects():
            keys = {p["key"] for p in _get(j.s, api + "/project", timeout)}
            if not keys:
                return ("warn", "no projects visible to this user")
            missing = [k for k in jc.project_keys if k.upper() not in keys]
            detail = f"{len(keys)} visible"
            if missing:
                return ("warn", detail + f"; configured but not visible: {', '.join(missing)}")
            return detail

        def custom_fields():
            for f in _get(j.s, api + "/field", timeout):
                fields[f["id"]] = f.get("name", "")
            problems, info = [], []
            for setting, wanted in (("epic_link_field", "epic link"), ("story_points_field", "story point")):
                fid = getattr(jc, setting)
                candidates = [f"{k} ({v})" for k, v in fields.items() if wanted in v.lower()]
                if fid and fid not in fields:
                    problems.append(f"{setting} = {fid} does not exist"
                                    + (f"; candidates: {', '.join(candidates)}" if candidates else ""))
                elif fid:
                    info.append(f"{setting} = {fid} ({fields[fid]})")
                elif candidates:
                    info.append(f"{setting} not set; candidates: {', '.join(candidates)}")
            if problems:
                return ("warn", "; ".join(problems + info))
            return "; ".join(info) or "ok"

        def link_types():
            types = _get(j.s, api + "/issueLinkType", timeout).get("issueLinkTypes", [])
            phrases = {p.lower() for t in types for p in (t.get("inward", ""), t.get("outward", ""))}
            missing = [p for p in jc.child_relations + jc.parent_relations if p.lower() not in phrases]
            if missing:
                avail = "; ".join(f"{t.get('name')}: '{t.get('outward')}' / '{t.get('inward')}'"
                                  for t in types)
                return ("warn", f"configured phrases not found: {', '.join(missing)}. "
                                f"Available (outward / inward): {avail}")
            return f"{len(types)} link types, all configured phrases exist"

        run.step("server reachable", server)
        run.step("authentication", myself)
        run.step("projects", projects, fatal=False)
        run.step("custom fields", custom_fields, fatal=False, warn_only=True)
        run.step("issue link types", link_types, fatal=False, warn_only=True)
    except Abort:
        pass
    except ConfigError as exc:
        res.steps.append(Step("fail", "configuration", error=str(exc)))
    return res


# ----------------------------------------------------------------------- LLM
PING_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok"],
               "properties": {"ok": {"type": "boolean"}}}


CONTEXT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["first", "last"],
                  "properties": {"first": {"type": "string"}, "last": {"type": "string"}}}
_WORD_FIRST, _WORD_LAST = "PELICAN-4821", "MARIGOLD-7395"
CONTEXT_HINT = ("The model only saw part of the prompt. Ollama: start the server with "
                "OLLAMA_CONTEXT_LENGTH=16384 (or larger), or create a model variant with "
                "'PARAMETER num_ctx 16384' in a Modelfile. Otherwise lower llm.max_input_chars and "
                "llm.chunk_chars so prompts fit the model's context window "
                "(roughly 3 characters per token for code).")


def is_ollama(url: str) -> bool:
    return ":11434" in url or "ollama" in url.lower()


def _filler(chars: int) -> str:
    """Deterministic code-like filler with realistic tokenisation."""
    import random

    rnd = random.Random(7)
    words = ["value", "result", "count", "index", "buffer", "config", "handler", "item", "total",
             "offset", "status", "record", "cache", "limit", "token", "entry", "node", "queue"]
    lines, size = [], 0
    while size < chars:
        a, b, c = rnd.choice(words), rnd.choice(words), rnd.choice(words)
        line = f"{a}_{rnd.randint(1, 999)} = compute_{b}({c}_{rnd.randint(1, 99)}, {rnd.randint(0, 9999)})"
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


def context_check(client, chars: int) -> tuple:
    from mrstat.estimate import truncation_note
    from mrstat.llm_client import parse_json_object

    msgs = [{"role": "system", "content": "You are a connectivity check. Answer with JSON only."},
            {"role": "user", "content":
                f"The first code word is {_WORD_FIRST}.\n\nFiller text follows; ignore it.\n"
                f"{_filler(chars)}\n\nThe last code word is {_WORD_LAST}.\n\n"
                'Reply with JSON: {"first": "<first code word>", "last": "<last code word>"}'}]
    sent = sum(len(m["content"]) for m in msgs)
    t0 = time.monotonic()
    res = client.chat(msgs, json_schema=CONTEXT_SCHEMA, schema_name="context_check")
    dt = time.monotonic() - t0
    try:
        data = parse_json_object(res.text)
    except LLMError:
        data = {}
    first_ok = _WORD_FIRST.lower() in str(data.get("first", "")).lower()
    last_ok = _WORD_LAST.lower() in str(data.get("last", "")).lower()
    detail = (f"{sent} characters sent (= llm.max_input_chars), server counted "
              f"{res.prompt_tokens or '?'} prompt tokens, {dt:.0f}s")
    note = truncation_note(msgs, res.prompt_tokens)
    if note or (last_ok and not first_ok):
        return ("fail", detail + "; the start of the prompt was lost (truncated)", CONTEXT_HINT)
    if first_ok and last_ok:
        return ("ok", detail + "; both ends of the prompt arrived")
    return ("warn", detail + f"; model did not repeat the code words ({res.text[:80]!r}) - "
                             "inconclusive, small models sometimes fail this", "")


def test_llm(cfg: Config, timeout: float, call: bool = True,
             check_context: bool | None = None) -> ServiceResult:
    from mrstat.llm_client import LLMClient, parse_json_object

    lc = cfg.llm
    res = ServiceResult("LLM", lc.base_url)
    run = _Runner(res, "API key missing or wrong: set MRSTAT_LLM_API_KEY. For Azure OpenAI set "
                       "llm.api_key_header = \"api-key\".")
    if not lc.base_url:
        run.add("skip", "not configured",
                hint="optional; set llm.base_url and llm.model to use `mrstat estimate`")
        return res
    try:
        client = LLMClient(dataclasses.replace(lc, timeout=max(timeout, min(lc.timeout, 180))))
        without_retries(client.s)

        def models():
            r = client.s.get(lc.base_url.rstrip("/") + "/models", params=lc.extra_query or None,
                             timeout=timeout)
            if r.status_code in (404, 405, 501):
                return ("skip", "endpoint has no /models list (fine, many gateways don't)")
            if r.status_code in (401, 403):
                r.raise_for_status()  # report as auth problem, but chat may still be tested
            r.raise_for_status()
            if "json" not in r.headers.get("Content-Type", "").lower():
                raise NotJSON(r)
            ids = [m.get("id") for m in (r.json().get("data") or []) if isinstance(m, dict)]
            if lc.model in ids:
                return f"model {lc.model} is listed"
            shown = ", ".join(map(str, ids[:15])) + (" ..." if len(ids) > 15 else "")
            return ("warn", f"model {lc.model} not in the list (may still work); available: {shown}")

        def chat():
            t0 = time.monotonic()
            out = client.chat(
                [{"role": "system", "content": "You are a connectivity check. Answer with JSON only."},
                 {"role": "user", "content": 'Reply with exactly {"ok": true}'}],
                json_schema=PING_SCHEMA, schema_name="ping")
            dt = time.monotonic() - t0
            data = parse_json_object(out.text)
            detail = (f"model {lc.model} answered in {dt:.1f}s "
                      f"({out.prompt_tokens} in / {out.completion_tokens} out tokens), "
                      f"response_format={lc.response_format}")
            if data.get("ok") is not True:
                return ("warn", detail + f"; unexpected content: {out.text[:100]!r}")
            return detail

        run.step("server reachable (/models)", models, fatal=False, warn_only=True)
        if not call:
            run.add("skip", "test completion", "skipped (--no-llm-call)")
            return res
        run.step("test completion (a few tokens)", chat)

        if check_context is None:
            check_context = lc.context_check == "always" or (
                lc.context_check == "auto" and is_ollama(lc.base_url))
        if check_context:
            long_client = LLMClient(lc)  # full llm.timeout: long prompts take a while locally
            without_retries(long_client.s)
            chars = min(lc.max_input_chars, 200_000)
            run.step(f"long prompt ({chars} characters)", lambda: context_check(long_client, chars),
                     fatal=False)
        else:
            run.add("skip", "long prompt", "not run (use --context-check; automatic for Ollama URLs)")
    except Abort:
        pass
    except ConfigError as exc:
        res.steps.append(Step("fail", "configuration", error=str(exc)))
    return res


# ----------------------------------------------------------------------- entry
def run_all(cfg: Config, only=SERVICES, timeout: float = 20.0, llm_call: bool = True,
            check_context: bool | None = None) -> list[ServiceResult]:
    out = []
    if "gitlab" in only:
        out.append(test_gitlab(cfg, timeout))
    if "jira" in only:
        out.append(test_jira(cfg, timeout))
    if "llm" in only:
        out.append(test_llm(cfg, timeout, llm_call, check_context))
    return out


_MARK = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]", "skip": "[SKIP]"}


def format_results(results: list[ServiceResult], cfg: Config) -> str:
    lines = [f"Configuration: {cfg.source or '(none - defaults and environment only)'}"]
    for res in results:
        lines += ["", f"{res.name}  {res.url or ''}"]
        for s in res.steps:
            text = s.title + (f": {s.detail}" if s.detail else "") + (f": {s.error}" if s.error else "")
            lines.append(f"  {_MARK[s.status]} {text}")
            if s.hint and s.status in ("fail", "warn", "skip"):
                lines.append(f"         -> {s.hint}")
    lines += ["", "Summary: " + ", ".join(f"{r.name} {r.status}" for r in results)]
    return "\n".join(lines)
