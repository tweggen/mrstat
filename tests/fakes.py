"""In-memory stand-ins for GitLab and Jira used by the tests."""

from __future__ import annotations

from mrstat.timeutil import parse_ts


def commit(sha, author, email, when, title="work", parents=1, message=None):
    return {"id": sha, "author_name": author, "author_email": email, "authored_date": when,
            "title": title, "message": message or title, "parent_ids": ["p"] * parents}


class FakeGitLab:
    def __init__(self):
        self.projects = {}
        self.mrs = {}  # (pid, iid) -> mr dict
        self.mr_commit_map = {}  # (pid, iid) -> [commit]
        self.repo = {}  # pid -> [commit]
        self.diffs = {}  # (pid, iid) -> [diff entries as returned by /diffs]

    def add_project(self, pid, path):
        self.projects[pid] = {"id": pid, "path_with_namespace": path, "web_url": f"https://gl/{path}"}

    def add_mr(self, pid, iid, commits, **kw):
        mr = {"id": pid * 1000 + iid, "iid": iid, "project_id": pid, "title": kw.get("title", f"MR {iid}"),
              "description": kw.get("description", ""), "state": kw.get("state", "merged"),
              "source_branch": kw.get("source_branch", f"feature/{iid}"),
              "target_branch": kw.get("target_branch", "main"),
              "created_at": kw.get("created_at", "2026-01-01T08:00:00Z"),
              "merged_at": kw.get("merged_at", "2026-01-10T12:00:00Z"),
              "updated_at": kw.get("updated_at", kw.get("merged_at", "2026-01-10T12:00:00Z")),
              "author": {"username": "dev"}, "web_url": f"https://gl/mr/{iid}",
              "time_stats": {"total_time_spent": kw.get("spent", 0), "time_estimate": 0}}
        self.mrs[(pid, iid)] = mr
        self.mr_commit_map[(pid, iid)] = commits
        self.repo.setdefault(pid, []).extend(commits)

    def project(self, ref):
        for p in self.projects.values():
            if str(ref) in (str(p["id"]), p["path_with_namespace"]):
                return p
        raise KeyError(ref)

    def merge_request(self, pid, iid):
        return self.mrs[(pid, iid)]

    def mr_commits(self, pid, iid):
        return iter(self.mr_commit_map[(pid, iid)])

    def list_mrs(self, *, project=None, group=None, updated_after=None, state="merged"):
        pid = self.project(project)["id"] if project is not None else None
        for (p, _), mr in sorted(self.mrs.items()):
            if pid is not None and p != pid:
                continue
            if updated_after and parse_ts(mr["updated_at"]) < updated_after:
                continue
            yield mr

    def mr_diffs(self, pid, iid):
        return self.diffs.get((pid, iid), [])

    def project_commits(self, pid, since, until, all_branches=True):
        for c in self.repo.get(pid, []):
            if since <= parse_ts(c["authored_date"]) <= until:
                yield c


class FakeJira:
    def __init__(self):
        self.issues = {}
        self.epics = {}  # epic key -> [child keys]
        self.wl = {}
        self.fetches = 0

    def add(self, key, *, type_="Story", spent=0, estimate=0, subtasks=(), parent=None,
            links=(), worklogs=()):
        """links: (phrase_from_this_issue, other_key, 'outward'|'inward')"""
        issuelinks = []
        for phrase, other, direction in links:
            if direction == "outward":
                issuelinks.append({"type": {"name": "X", "outward": phrase, "inward": "-"},
                                   "outwardIssue": {"key": other}})
            else:
                issuelinks.append({"type": {"name": "X", "outward": "-", "inward": phrase},
                                   "inwardIssue": {"key": other}})
        self.issues[key] = {"key": key, "fields": {
            "summary": f"summary {key}", "description": f"description of {key}", "issuetype": {"name": type_}, "status": {"name": "Done"},
            "subtasks": [{"key": s} for s in subtasks], "parent": {"key": parent} if parent else None,
            "issuelinks": issuelinks, "timespent": spent or None, "aggregatetimespent": None,
            "timeoriginalestimate": estimate or None, "aggregatetimeoriginalestimate": None}}
        self.wl[key] = [{"id": f"{key}-{i}", "author": {"name": who, "emailAddress": email},
                         "started": "2026-01-05T09:00:00.000+0100", "timeSpentSeconds": secs}
                        for i, (who, email, secs) in enumerate(worklogs)]

    def browse_url(self, key):
        return f"https://jira/browse/{key}"

    def project_keys(self):
        return sorted({k.split("-")[0] for k in self.issues})

    def issue(self, key, fields):
        self.fetches += 1
        return self.issues.get(key)

    def search_keys(self, jql):
        key = jql.split("=")[-1].strip()
        return self.epics.get(key, [])

    def worklogs(self, key):
        return iter(self.wl.get(key, []))


class FakeLLM:
    """Stands in for LLMClient: records calls, returns canned answers in order."""

    def __init__(self, answers, model="fake-model"):
        self.answers = list(answers)
        self.calls = []
        self.model = model

    def chat(self, messages, *, json_schema=None, schema_name="result"):
        from mrstat.llm_client import ChatResult

        self.calls.append({"messages": messages, "schema": json_schema})
        return ChatResult(self.answers.pop(0), 100, 20)
