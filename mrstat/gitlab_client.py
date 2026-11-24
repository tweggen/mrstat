"""Minimal GitLab REST v4 client (self-hosted)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator
from urllib.parse import quote

import requests

from mrstat.config import ConfigError, GitLabConfig
from mrstat.http import make_session
from mrstat.timeutil import iso


class GitLabClient:
    def __init__(self, cfg: GitLabConfig):
        if not cfg.url:
            raise ConfigError("gitlab.url is not configured (or MRSTAT_GITLAB_URL)")
        self.api = cfg.url.rstrip("/") + "/api/v4"
        headers = {"PRIVATE-TOKEN": cfg.token} if cfg.token else {}
        self.s = make_session(cfg.verify_ssl, headers)
        self.timeout = cfg.timeout

    # -- plumbing -------------------------------------------------------------------
    def _get(self, path: str, params: dict | None = None):
        r = self.s.get(self.api + path, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r

    def get(self, path: str, params: dict | None = None) -> Any:
        return self._get(path, params).json()

    def paged(self, path: str, params: dict | None = None) -> Iterator[dict]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        page = 1
        while True:
            params["page"] = page
            r = self._get(path, params)
            items = r.json()
            yield from items
            nxt = r.headers.get("X-Next-Page")
            if not nxt or not items:
                break
            page = int(nxt)

    @staticmethod
    def _ref(ref: str | int) -> str:
        return quote(str(ref), safe="")

    # -- API --------------------------------------------------------------------------
    def version(self) -> dict:
        return self.get("/version")

    def project(self, ref: str | int) -> dict:
        return self.get(f"/projects/{self._ref(ref)}")

    def merge_request(self, project_id: int, iid: int) -> dict:
        return self.get(f"/projects/{project_id}/merge_requests/{iid}")

    def mr_commits(self, project_id: int, iid: int) -> Iterator[dict]:
        return self.paged(f"/projects/{project_id}/merge_requests/{iid}/commits")

    def mr_diffs(self, project_id: int, iid: int) -> list[dict]:
        """Combined diff of the MR (latest version), one entry per file.
        Uses /diffs (GitLab >= 15.7), falls back to the older /changes endpoint."""
        try:
            return list(self.paged(f"/projects/{project_id}/merge_requests/{iid}/diffs"))
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
        data = self.get(f"/projects/{project_id}/merge_requests/{iid}/changes")
        return data.get("changes", [])

    def list_mrs(
        self,
        *,
        project: str | int | None = None,
        group: str | int | None = None,
        updated_after: datetime | None = None,
        state: str = "merged",
    ) -> Iterator[dict]:
        params: dict[str, Any] = {"scope": "all", "order_by": "updated_at", "sort": "asc"}
        if state and state != "all":
            params["state"] = state
        if updated_after:
            params["updated_after"] = iso(updated_after)
        if project is not None:
            path = f"/projects/{self._ref(project)}/merge_requests"
        elif group is not None:
            path = f"/groups/{self._ref(group)}/merge_requests"
        else:
            raise ValueError("project or group required")
        return self.paged(path, params)

    def project_commits(
        self, project_id: int, since: datetime, until: datetime, all_branches: bool = True
    ) -> Iterator[dict]:
        params = {"since": iso(since), "until": iso(until), "all": str(all_branches).lower()}
        return self.paged(f"/projects/{project_id}/repository/commits", params)
