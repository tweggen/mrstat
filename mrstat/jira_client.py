"""Minimal Jira Server / Data Center REST v2 client."""

from __future__ import annotations

import logging
from typing import Any, Iterator

from mrstat.config import ConfigError, JiraConfig
from mrstat.http import make_session

log = logging.getLogger(__name__)


_JIRA_UI_PARTS = ("/rest/api/", "/browse/", "/secure/", "/projects/", "/issues/", "/login.jsp")


def normalize_base_url(url: str) -> str:
    """Strip trailing slashes and anything copied from the browser after the base URL:
    'https://host/jira/browse/ABC-1' -> 'https://host/jira'."""
    url = url.strip()
    for part in _JIRA_UI_PARTS:
        i = url.find(part)
        if i > 0:
            url = url[:i]
    return url.rstrip("/")


class JiraClient:
    def __init__(self, cfg: JiraConfig):
        if not cfg.url:
            raise ConfigError("jira.url is not configured (or MRSTAT_JIRA_URL)")
        self.url = normalize_base_url(cfg.url)
        self.api = self.url + "/rest/api/2"
        headers = {"Accept": "application/json"}
        auth = None
        if cfg.username:
            auth = (cfg.username, cfg.password or cfg.token)
        elif cfg.token:
            headers["Authorization"] = f"Bearer {cfg.token}"
        self.s = make_session(cfg.verify_ssl, headers, auth)
        self.timeout = cfg.timeout

    def browse_url(self, key: str) -> str:
        return f"{self.url}/browse/{key}"

    def _get(self, path: str, params: dict | None = None) -> Any:
        r = self.s.get(self.api + path, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def server_info(self) -> dict:
        return self._get("/serverInfo")

    def myself(self) -> dict:
        return self._get("/myself")

    def project_keys(self) -> list[str]:
        return [p["key"] for p in self._get("/project")]

    def issue(self, key: str, fields: list[str]) -> dict | None:
        r = self.s.get(
            f"{self.api}/issue/{key}", params={"fields": ",".join(fields)}, timeout=self.timeout
        )
        if r.status_code in (403, 404):
            return None
        r.raise_for_status()
        return r.json()

    def search_keys(self, jql: str) -> list[str]:
        keys: list[str] = []
        start = 0
        while True:
            r = self.s.post(
                f"{self.api}/search",
                json={"jql": jql, "startAt": start, "maxResults": 100, "fields": ["key"]},
                timeout=self.timeout,
            )
            if r.status_code == 400:  # e.g. "Epic Link" field unknown on this instance
                log.warning("JQL rejected (%s): %s", jql, r.text[:300])
                return keys
            r.raise_for_status()
            data = r.json()
            issues = data.get("issues", [])
            keys += [i["key"] for i in issues]
            start += len(issues)
            if not issues or start >= data.get("total", 0):
                return keys

    def worklogs(self, key: str) -> Iterator[dict]:
        start = 0
        while True:
            data = self._get(f"/issue/{key}/worklog", {"startAt": start, "maxResults": 1000})
            logs = data.get("worklogs", [])
            yield from logs
            start += len(logs)
            if not logs or start >= data.get("total", 0):
                return
