"""Configuration: TOML file + environment variable overrides (secrets belong in env vars)."""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    pass


@dataclass
class GitLabConfig:
    url: str = ""
    token: str = ""
    # True, False, or a path to a CA bundle (typical for internal enterprise CAs).
    verify_ssl: bool | str = True
    timeout: float = 60.0
    # Scope for `sync`: project paths/ids and/or group paths/ids.
    projects: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    mr_state: str = "merged"  # merged | opened | closed | all
    # Only consider MRs into these branches during sync (empty = all). Avoids double counting
    # when the same commits flow develop -> main via several MRs.
    target_branches: list[str] = field(default_factory=list)


@dataclass
class JiraConfig:
    url: str = ""
    token: str = ""  # Personal Access Token (Bearer) - Jira Server/DC >= 8.14
    username: str = ""  # if set: basic auth username + (password or token)
    password: str = ""
    verify_ssl: bool | str = True
    timeout: float = 60.0
    # Restrict key extraction to these project keys. Empty + auto_discover -> ask Jira.
    project_keys: list[str] = field(default_factory=list)
    auto_discover_projects: bool = True
    story_points_field: str = ""  # e.g. "customfield_10002"
    epic_link_field: str = ""  # e.g. "customfield_10008" (Jira Server "Epic Link")
    # JQL to find children of an epic; "{key}" is replaced. Empty disables.
    epic_children_jql: str = '"Epic Link" = {key}'
    # Link phrases as seen *from the parent* ("A requires B" -> B is a child of A).
    child_relations: list[str] = field(
        default_factory=lambda: ["requires", "contains", "is parent of"]
    )
    # Link phrases as seen *from the child* ("B is required by A" -> A is a parent of B).
    parent_relations: list[str] = field(
        default_factory=lambda: ["is required by", "is contained in", "is child of"]
    )
    ascend_levels: int = 0  # climb this many parent levels from referenced tickets
    max_depth: int = 6  # how deep to descend below the roots
    include_subtasks: bool = True
    fetch_worklogs: bool = True
    cache_hours: float = 12.0  # re-fetch an issue only if older than this


@dataclass
class EffortConfig:
    max_gap_minutes: float = 120.0  # commits closer than this belong to one work session
    first_commit_minutes: float = 60.0  # assumed work before the first commit of a session
    max_hours_per_day: float = 10.0  # cap per person and calendar day (0 = no cap)
    hours_per_day: float = 8.0  # for converting hours to person days
    timezone: str = "UTC"  # defines "calendar day" for the cap
    ignore_merge_commits: bool = True
    ignore_authors: list[str] = field(default_factory=list)  # regexes on name/email
    fetch_context: bool = True  # single-MR mode: fetch the authors' other commits too


@dataclass
class Config:
    db_path: str = "mrstat.sqlite"
    key_sources: list[str] = field(
        default_factory=lambda: ["title", "description", "source_branch", "commits"]
    )
    gitlab: GitLabConfig = field(default_factory=GitLabConfig)
    jira: JiraConfig = field(default_factory=JiraConfig)
    effort: EffortConfig = field(default_factory=EffortConfig)
    # canonical person -> aliases (git emails, git names, jira usernames, jira emails)
    identities: dict[str, list[str]] = field(default_factory=dict)
    source: str | None = None


def _fill(cls: type, data: dict[str, Any], section: str):
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")
    return cls(**data)


_ENV = {
    "MRSTAT_DB": ("", "db_path"),
    "MRSTAT_GITLAB_URL": ("gitlab", "url"),
    "MRSTAT_GITLAB_TOKEN": ("gitlab", "token"),
    "MRSTAT_JIRA_URL": ("jira", "url"),
    "MRSTAT_JIRA_TOKEN": ("jira", "token"),
    "MRSTAT_JIRA_USERNAME": ("jira", "username"),
    "MRSTAT_JIRA_PASSWORD": ("jira", "password"),
}


def load(path: str | os.PathLike | None = None) -> Config:
    if path is None:
        path = os.environ.get("MRSTAT_CONFIG")
        if path is None and Path("mrstat.toml").exists():
            path = "mrstat.toml"
    data: dict[str, Any] = {}
    if path is not None:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
        with p.open("rb") as fh:
            data = tomllib.load(fh)

    sections = {
        "gitlab": _fill(GitLabConfig, data.pop("gitlab", {}), "gitlab"),
        "jira": _fill(JiraConfig, data.pop("jira", {}), "jira"),
        "effort": _fill(EffortConfig, data.pop("effort", {}), "effort"),
        "identities": data.pop("identities", {}),
    }
    data.pop("source", None)
    cfg = _fill(Config, {**data, **sections}, "top level")
    cfg.source = str(path) if path else None

    for var, (section, attr) in _ENV.items():
        value = os.environ.get(var)
        if value:
            setattr(getattr(cfg, section) if section else cfg, attr, value)
    return cfg
