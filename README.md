# mrstat

Estimates the effort (person hours / person days, per contributor) behind GitLab merge
requests from their commits, links MRs to Jira tickets, and compares the MR effort with
the time booked in the related Jira ticket hierarchy.

Runs as a CLI (good for testing single MRs, e.g. on Windows) or as a container with a
small web UI and a periodic incremental sync that builds up a statistics database (SQLite).

## How it works

### 1. Effort from commits

Git has no time information, so effort is estimated from each person's stream of commits
(authored dates, so rebases don't distort it):

* The gap to the person's previous commit counts as work on this commit if it is at most
  `max_gap_minutes` (default 120).
* Otherwise a new session starts and the commit gets `first_commit_minutes` (default 60).
* Per person and calendar day the total is capped at `max_hours_per_day` (default 10).

The stream is **per person across all MRs and projects in the database**, so interleaved
work is split correctly: time between a commit for MR A and the next commit for MR B
goes to B. That's why single-MR mode also fetches the project's other commits around the
MR (`fetch_context`), and why `sync` computes efforts only after loading everything.
Cherry-picks keep the authored date, so backport MRs cost nearly nothing. Merge commits
and bots (`ignore_authors`) are ignored.

Person days = hours / `hours_per_day`. GitLab `/spend` time tracking, if used, is shown
alongside.

### 2. MR → Jira

Keys (`ABC-123`) are taken from the MR title, description, source branch and commit
messages. Only keys of real Jira projects count (so `UTF-8` is ignored).

### 3. Hierarchy and the n:m comparison

Starting from a set of MRs:

1. **referenced** = keys mentioned by the MRs (+ `ascend_levels` levels of parents)
2. **roots** = minimal subset: keys that lie below another referenced key are dropped
3. **closure** = roots + all descendants: subtasks, epic children, and issue links whose
   phrase is in `child_relations` (e.g. *A requires B* → B is below A)
4. **cluster**: all known MRs referencing anything in the closure are added, and steps
   1–3 repeat until stable (switch off with `--no-expand`)

Then: sum of MR effort vs. sum of each closure issue's **own** time spent (each issue
counted once, so no double counting via aggregate fields), overall and per person
(from worklogs), plus original estimates and story points.

Expect the ratio to be < 1: Jira time typically includes analysis, reviews, testing and
meetings that produce no commits. The ratio is most useful compared across teams/time.

## Install and try (Windows 11 or Linux)

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -e .[web]
copy mrstat.example.toml mrstat.toml     # edit URLs, projects, Jira fields
$env:MRSTAT_GITLAB_TOKEN = "glpat-..."   # read_api scope is sufficient
$env:MRSTAT_JIRA_TOKEN   = "..."         # Jira PAT (or MRSTAT_JIRA_USERNAME/PASSWORD)

mrstat check
mrstat mr https://gitlab.example.internal/product/backend/-/merge_requests/1234 --tree
mrstat mr product/backend!1234 --json
mrstat issue APP-4711
```

Internal CA: set `verify_ssl = "C:/path/ca.pem"` in the config (or `REQUESTS_CA_BUNDLE`).

## Statistics database

```bash
mrstat sync --since 2026-01-01 --until 2026-07-01   # historical (MRs merged in the span)
mrstat sync --incremental                           # from the last successful sync
mrstat report --by person --since 2026-01-01 --csv persons.csv
mrstat recompute                                    # after changing [effort] or [identities]
mrstat serve --port 8080                            # web UI on http://127.0.0.1:8080
```

## Container

```bash
cp mrstat.example.toml mrstat.toml   # edit
export MRSTAT_GITLAB_TOKEN=... MRSTAT_JIRA_TOKEN=...
docker compose up -d --build
```

The container serves the UI on port 8080 and runs an incremental sync every
`MRSTAT_SYNC_EVERY` seconds (first run from `MRSTAT_INITIAL_SINCE`). Data lives in the
`/data` volume. JSON API: `/api/mrs`, `/api/persons`, `/api/mr/{project_id}/{iid}`,
`/api/issue/{KEY}`, `/api/sync`.

The web UI has no authentication; put it behind your reverse proxy / SSO if needed.

## Things to set up per instance

* **Jira link phrases** (`child_relations` / `parent_relations`) – use the exact
  outward/inward texts of your link types (Jira admin → Issue linking).
* **Epic Link field** id (`epic_link_field`), and `epic_children_jql` (newer Jira DC
  versions use `parent = {key}`).
* **Identities** – if git emails and Jira users differ, map them in `[identities]`,
  otherwise per-person comparisons won't line up.
* **target_branches** – if commits travel through several MRs (feature → develop → main),
  restrict to one level, otherwise effort is counted once per MR.

## Development

```bash
pip install -e .[web,dev]
pytest
```
