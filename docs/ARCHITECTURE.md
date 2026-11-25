# mrstat – requirements and architecture

This is the reference document for humans and coding agents working on mrstat. User-facing
usage is in [README.md](../README.md). Keep this file up to date when requirements,
decisions or module boundaries change.

## 1. Purpose

A mid-sized department develops enterprise software with **self-hosted GitLab** and
**self-hosted Jira**. mrstat answers these questions:

1. **MR effort:** how much effort (person hours / person days) went into the commits of
   a merge request, and how is it split between the people who contributed?
2. **MR ↔ Jira:** MRs and Jira tickets are linked n:m. For the minimal set of tickets
   behind a set of MRs, how does the computed MR effort compare with the effort recorded
   in the Jira ticket hierarchy (subtasks, epics, links such as "requires")?
3. **LLM estimate:** how does an LLM's estimate of the effort behind the *final change*
   compare with the Jira estimates, Jira booked time and commit-based effort?

## 2. Requirements

### Functional

| ID | Requirement |
|---|---|
| F1 | Analyze a single MR on demand (by URL, `group/project!iid`, or project + iid), mainly for testing. |
| F2 | Run historically over a time span, and incrementally from the last successful run, building a statistics database. |
| F3 | Estimate effort per MR and per person from commits. |
| F4 | Extract Jira keys from MR title, description, source branch and commit messages. |
| F5 | Build the Jira hierarchy below the referenced tickets (subtasks, epic children, configurable link phrases) and compare MR effort with Jira effort over the n:m cluster, overall and per person. |
| F6 | Show results in a web UI and a JSON API; export CSV reports. |
| F7 | Test the configuration (`mrstat test`), with failures classified by cause and with a hint. |
| F8 | LLM estimate of an MR's combined diff via any OpenAI-compatible `/chat/completions` endpoint (configurable, no vendor SDK). |
| F9 | (planned) Evaluate estimate variants statistically against commit hours and Jira booked time. |

### Non-functional

| ID | Requirement |
|---|---|
| N1 | Python ≥ 3.11, portable. **Testing** runs on a Windows 11 notebook in the corporate network. **Production** is a Linux container on a server in the corporate network. |
| N2 | The developer's machine (where agents run) has **no access** to GitLab, Jira or the internal LLM. All tests use fakes or local stub servers. Feedback arrives as pasted `mrstat test` / dry-run output. |
| N3 | Few dependencies: `requests` (core); `fastapi`, `uvicorn`, `jinja2` (web extra). Storage is SQLite (one file). |
| N4 | Secrets only via environment variables (`MRSTAT_*_TOKEN`, `MRSTAT_LLM_API_KEY`), never in the config file or in output. Internal CAs via `verify_ssl` = CA bundle path. |
| N5 | Company data is confidential. Nothing is sent anywhere except the configured GitLab, Jira and LLM endpoints. `--dry-run` shows exactly what would be sent to the LLM. |
| N6 | Errors in one MR must not abort a sync. The incremental marker only advances on a clean run. |

### Decisions taken with the user (don't revert without asking)

* **Effort from commits** uses the session heuristic (§4.1) on *authored* dates, per person
  across all MRs/projects.
* **Jira comparison** counts each issue's *own* time spent once over the closure, never
  aggregate fields (no double counting).
* **LLM input is the combined diff** of the MR (target → final result), not per-commit
  diffs. It's acceptable that this hides trial and error; the gap between the LLM estimate
  and the real hours *is* the hidden effort.
* **No commit timeline** is sent to the LLM.
* **Blind prompt:** the LLM never sees Jira estimates, story points, booked time or
  commit-based hours, so it can't anchor on them.
* **The goal is correlation, not accuracy.** The LLM result doesn't need to match real
  hours; it needs to correlate with them, ideally linearly. Consistency (same change →
  same result) matters more than absolute calibration. Never mix models or prompt versions
  within one analysis.
* **The LLM is configurable via the OpenAI-compatible API** (gateway, Azure OpenAI,
  Ollama, vLLM, …). Small local models (e.g. `gemma3:12b` via Ollama) must be supported,
  hence deterministic preprocessing and context-window checks.

## 3. Architecture overview

```
              CLI (cli.py)                         Web UI + JSON API (web/app.py)
     test | mr | issue | estimate | sync |              (+ periodic sync thread)
       recompute | report | serve
                 │                                             │
   ┌─────────────┼───────────────┬───────────────┐             │
   ▼             ▼               ▼               ▼             ▼
selftest.py  analyze.py       sync.py       estimate.py    compare.py / queries.py / report.py
 (checks)   (MR → commits,   (time span,    (diff → clean    (read-only aggregation
             keys, Jira tree, incremental)   → prompt → LLM)   over the DB)
             effort)              │               │
   │             │                │               ├── diffprep.py (cleanup + facts)
   │             ├── effort.py ───┘               └── llm_client.py (OpenAI-compatible)
   │             ├── keys.py
   │             └── identities.py
   ▼             ▼
gitlab_client.py   jira_client.py      (thin REST clients on http.py sessions)
                 │
                 ▼
            db.py (SQLite schema, migrations, writers)
```

Data flow: **fetch** (clients) → **store raw facts** (db) → **compute** (effort, closure,
estimates) → **read/aggregate** (compare, queries) → **present** (CLI, web).
Computation reads from the DB, not from the APIs, so it can be re-run offline
(`recompute`).

## 4. Components

### 4.1 Effort from commits – `effort.py`, `analyze.py`

* Per person, commits are sorted by authored date. A gap ≤ `max_gap_minutes` (120)
  counts as work; otherwise a new session starts with `first_commit_minutes` (60).
  Per person and calendar day the total is capped at `max_hours_per_day` (10) in the
  configured timezone. Person days = hours / `hours_per_day`.
* The stream is global per person (all MRs and projects in the DB), so interleaved work is
  split correctly. Single-MR mode therefore also fetches the project's commits in a window
  around the MR (`fetch_context`). `sync` computes efforts only after all MRs and context
  commits are stored.
* Merge commits and bots (`ignore_authors`) are excluded. Cherry-picks keep the authored
  date → ~0 h. Same SHA in several MRs is counted in each (use `gitlab.target_branches`).
* Identities: `identities.py` maps git emails/names and Jira users to one canonical person.

### 4.2 Jira hierarchy – `analyze.py` (fetch), `compare.py` (closure)

* `fetch_issue_tree`: referenced keys (+ `ascend_levels` parents), then BFS down to
  `max_depth` via subtasks, epic children (`epic_children_jql`) and link phrases in
  `child_relations` / `parent_relations` (phrases as seen from parent / child). Edges are
  stored normalised as parent→child with `discovered_by`; issues are cached for
  `cache_hours`. Missing issues are stored with `found = 0`.
* `compare()`: referenced → ancestors → **minimal roots** (drop keys below other keys) →
  closure → all known MRs referencing the closure; with `expand`, repeat to a fixed point
  (the n:m cluster). Totals: MR hours, Jira own time spent (worklogs per person, else
  `timespent`), estimates, story points, GitLab `/spend`, LLM estimates.

### 4.3 Sync – `sync.py`

Collects merged MRs from configured projects/groups (`updated_after` + merged-at filter),
analyzes each with `fetch_context=False`, then fetches context commits once per project
window, then computes efforts. The incremental state `last_sync_until` (with 1 h overlap)
advances only if no MR failed.

### 4.4 LLM estimate – `estimate.py`, `diffprep.py`, `llm_client.py`

1. **Input:** MR text, referenced tickets (summary + description, clipped), and the MR diff
   from GitLab `/merge_requests/:iid/diffs` (fallback `/changes`).
2. **Filter** (`classify_files`): exclude globs (lock files, vendor, generated, binaries),
   GitLab `generated_file`/`too_large`, deleted files. Excluded files are listed, not sent.
3. **Clean** (`diffprep.clean`, deterministic): whitespace-only pairs and blank lines,
   moved blocks (≥ `min_move_block` substantial lines, across or within files, re-indent
   tolerant) → `~ [N lines moved to/from X]` markers, comment-only and import lines,
   context trimmed to `context_lines`. Per-file **facts** (category, language, raw/shown
   counts, noise counts, functions from `@@` headers) form a table in the prompt.
4. **Size:** above `max_input_chars` → `large_mr_strategy`: `summarize` (chunks of
   `chunk_chars`, one summary call each, then the estimate from summaries), `truncate`
   (with visible markers) or `skip`. Never cut silently.
5. **Call:** system prompt + user prompt; JSON schema via `response_format`
   (`json_schema` | `json_object` | `none`); tolerant JSON parsing; one retry on an
   invalid answer. Truncation is flagged if the server counts far fewer prompt tokens than
   chars/7.
6. **Store:** `llm_estimates` keyed by MR, model, `PROMPT_VERSION`, input hash. The same
   input isn't re-estimated unless `--force`. `Estimator.run()` processes a batch and
   counts new / unchanged / skipped / failed; `Estimator.missing()` lists MRs without an
   estimate for the current model and `PROMPT_VERSION` (DB only). `sync --llm`,
   `serve --llm` and `estimate --missing` estimate the run's MRs plus the missing ones,
   so incremental runs catch up on MRs synced without `--llm`. **Bump `PROMPT_VERSION` whenever prompt
   text or preprocessing changes what is sent.**

`llm_client.py` is plain `requests` against `{base_url}/chat/completions` with configurable
auth header, extra query/headers/body, `max_tokens_param` and `temperature = "omit"`.

### 4.5 Configuration test – `selftest.py`

Steps per service, without retries (first error shown). `classify()` maps exceptions to
categories + hints: DNS, refused, timeout, TLS/internal CA, proxy, HTML/SSO page instead of
JSON (`NotJSON`), HTTP 401/403/404/407/429/5xx, LLM 400 (response_format etc.). Checks
GitLab version/user/token scopes/projects/groups; Jira serverInfo/myself/projects/custom
fields/link phrases; LLM `/models`, a ping completion, and a long-prompt **context check**
(code words at both ends; automatic for Ollama URLs).

### 4.6 Progress – `progress.py`

`Progress` interface (`phase`, `advance`, `note`, `done`) passed into `run_sync` /
`recompute_all`: `ConsoleProgress` (`bar` = in-place line on stderr, auto-selected in a
TTY or Git Bash via `MSYSTEM`; `lines` = one line per item), `StatusProgress` (writes into
the web UI's sync status dict). In bar mode, log handlers write through
`ProgressAwareStream`, so log lines don't garble the status line.

### 4.7 Presentation – `report.py`, `queries.py`, `web/`

Text/JSON output for the CLI; SQL for MR lists and per-person stats; FastAPI + Jinja2
templates (MR list, MR detail, issue/cluster, persons) and `/api/*`. The web UI has no
authentication (run it behind a proxy/SSO). The serve command can run incremental syncs
(optionally with `--llm`) in a background thread.

## 5. Data model (SQLite, `db.py`)

| Table | Content |
|---|---|
| `projects` | GitLab project id → path |
| `mrs` | MR metadata, GitLab time tracking, `analyzed_at` |
| `commits` | sha, canonical author, authored_at (UTC `…Z` text, sortable) |
| `mr_commits` | MR ↔ commit |
| `mr_effort` | computed hours/commits per MR and person |
| `mr_issues` | MR ↔ Jira key, with source (title/branch/description/commit) |
| `issues` | Jira issue fields incl. description, own/aggregate time spent and estimates, story points, `found`, `fetched_at` |
| `issue_links` | normalised parent→child edges with relation and `discovered_by` |
| `worklogs` | Jira worklogs per person |
| `llm_estimates` | LLM results (low/likely/high, breakdown, rationale, strategy, tokens, notes) per MR/model/prompt version/input hash |
| `sync_state` | `last_sync_until` |

Schema changes: add tables in `SCHEMA` (`CREATE … IF NOT EXISTS`); add columns to
existing tables via `MIGRATIONS` (applied with `ALTER TABLE` on start). Users already have
databases, so never rename or drop columns.

## 6. Conventions

* All timestamps are stored as UTC `YYYY-MM-DDTHH:MM:SSZ` via `timeutil.iso/norm`.
* Config: dataclasses in `config.py`. Unknown keys are an error. Secrets and URLs can be
  overridden by `MRSTAT_*` environment variables. Document new settings in
  `mrstat.example.toml` and README.
* Clients raise `requests` exceptions. The CLI maps them to exit codes (2 config/usage,
  3 HTTP/connection, 4 LLM).
* Output must never contain tokens or API keys.
* Windows: no POSIX-only APIs, `pathlib` for paths, `tzdata` dependency for zoneinfo,
  ASCII status markers in console output.

## 7. Testing

`pytest` (`pip install -e .[web,dev]`). No test may need network access to real services.

* `tests/fakes.py`: `FakeGitLab`, `FakeJira`, `FakeLLM` (in-memory, token counting,
  optional simulated context limit).
* `tests/test_selftest.py`: a local `ThreadingHTTPServer` stub mimicking GitLab, Jira and
  an LLM gateway (including a fake Ollama that truncates).
* Real-world validation happens on the user's Windows notebook (PowerShell or Git Bash, see
  README "Testing on Windows 11 with Git Bash"): `mrstat test`,
  `mrstat estimate … --dry-run --dump-prompt`. The user pastes the output back.

## 8. Roadmap

Done: commit effort, Jira closure and cluster comparison, sync, web UI, `mrstat test`, LLM
estimate (holistic, OpenAI-compatible), diff cleanup + facts table, context check.

Next (agreed order):

1. **Per-file mode** (`llm.mode = "per_file"`): one short call per file or file group
   returning categorical ratings (kind, difficulty 1–5, amount of new logic, risk
   factors). Fixed rubric, temperature 0 or median of several runs. Code combines the
   ratings into a **unitless effort score** with fixed, documented weights.
2. **`mrstat llm-eval`**: for a set of historical MRs and one model + prompt version,
   report Pearson (linear and log-log), Spearman, regression slope/intercept/R²,
   robustness without the top 5%, against commit hours and Jira booked time (and their
   mutual correlation as a ceiling), also aggregated per cluster/month/team; scatter plot
   in the web UI. If weights are fitted, use a train/test split or fit and evaluate
   against different targets.

Proposed earlier, not built: `mrstat discover` (suggest instance-specific Jira settings;
partly covered by `mrstat test`), `--record/--replay` of API responses for reproducing the
user's cases offline, `--redact` (pseudonymise people, blank texts) for sharing such
recordings.
