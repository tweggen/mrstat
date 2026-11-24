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

### 4. LLM estimate of the final change (optional)

An LLM gets the MR's **combined diff** (target branch → final result), the MR text and the
referenced tickets' summary and description, and estimates the hours an experienced
developer who knows the codebase would need for exactly this change (low / likely / high,
with a breakdown and rationale).

* **Blind:** the model never sees Jira estimates, story points, booked time or the commit
  heuristic, so it can't anchor on them.
* **Final change only:** trial and error that left no trace in the result is deliberately
  not part of the estimate. The gap between LLM estimate and commit/Jira hours is
  therefore the effort the result doesn't show (searching, rework, reviews, coordination).
* **Filtered input:** lock files, vendored and generated code, binaries and deleted files
  are listed with their line counts but their content is not sent (`llm.exclude`).
* **Cleaned diff:** before sending, the diff is cleaned deterministically, so the same kind
  of change always looks the same to the model:
  * whitespace-only changes and added/removed blank lines are dropped,
  * moved code (blocks of at least 3 substantial lines removed in one place and added
    elsewhere, also re-indented) becomes one line `~ [N lines moved to/from ...]`,
  * comment-only and import/include lines are dropped,
  * context is reduced to 1 line, and `@@ name` lines say which function a change is in.

  Nothing disappears from view: a **facts table** lists, per file, the category (code,
  test, config, docs), language, raw size, what is still shown, and what was removed as
  noise. Each setting can be switched off in `[llm]`.
* **Truncation warning:** if the server reports far fewer prompt tokens than the prompt
  should have, the estimate is flagged as "input probably truncated" (typical for a local
  model whose context window is smaller than the prompt).
* **Large MRs:** above `max_input_chars` the diff is summarized per file group first and
  the estimate is made from the summaries (`large_mr_strategy`). Nothing is cut silently;
  the stored estimate records the strategy used.
* **Cached:** each estimate is stored with a hash of its input, model and prompt version,
  and only recomputed when one of them changes (or with `--force`).

Any OpenAI-compatible `/chat/completions` endpoint works (internal gateway, Azure OpenAI,
vLLM, Ollama, LiteLLM, ...). Source code is sent to that endpoint, so use one your company
has approved; `--dry-run` shows exactly what would be sent.

## Install (Windows 11 or Linux)

```powershell
git clone https://github.com/tweggen/mrstat.git
cd mrstat
py -m venv .venv
.venv\Scripts\activate          # Linux: source .venv/bin/activate
pip install -e .[web]
```

To update later: `git pull` and `pip install -e .[web]` again.

## Configure GitLab and Jira

You set the two server URLs in a config file and pass the tokens as environment variables.
mrstat adds the API paths itself (`/api/v4` for GitLab, `/rest/api/2` for Jira), so you
only give the base URL you see in the browser.

### 1. Config file

```powershell
copy mrstat.example.toml mrstat.toml
notepad mrstat.toml
```

The minimum to edit:

```toml
[gitlab]
url = "https://gitlab.yourcorp.internal"          # base URL, no /api/v4
projects = ["group/subgroup/project"]             # only needed for `sync`

[jira]
url = "https://jira.yourcorp.internal"            # if Jira lives under a path: "https://host/jira"
```

- **URL format:** use the address as it appears in the browser, without a trailing path.
  For Jira, if issues open at `https://host/jira/browse/ABC-1`, the URL is `https://host/jira`.
- **Location:** mrstat reads `mrstat.toml` from the current directory. Otherwise pass
  `-c path\to\file.toml`, or set `MRSTAT_CONFIG`.
- **`projects`:** use the path from the project's URL (`group/subgroup/project`) or the
  numeric project ID. Single-MR runs (`mrstat mr <URL>`) don't need it.

`mrstat.toml` is in `.gitignore`, so your local settings are never committed.

### 2. Tokens (as environment variables, not in the file)

- **GitLab:** create a personal access token under *Avatar → Edit profile → Access Tokens*
  with the scope `read_api`.
- **Jira Server/Data Center:** create a personal access token under
  *Avatar → Profile → Personal Access Tokens*. This needs Jira 8.14 or later.

```powershell
$env:MRSTAT_GITLAB_TOKEN = "glpat-xxxxxxxx"
$env:MRSTAT_JIRA_TOKEN   = "xxxxxxxx"
```

If your Jira has no personal access tokens, use basic auth instead:

```powershell
$env:MRSTAT_JIRA_USERNAME = "your.login"
$env:MRSTAT_JIRA_PASSWORD = "..."
```

`$env:` only lasts for the current PowerShell window. To keep a variable permanently, run
`setx MRSTAT_GITLAB_TOKEN "glpat-..."` and open a new window. You can also override the
URLs per session with `MRSTAT_GITLAB_URL` and `MRSTAT_JIRA_URL`, and the database path
with `MRSTAT_DB`. (Linux: `export MRSTAT_GITLAB_TOKEN=...`.)

### 3. Corporate certificates

If you get SSL errors (`CERTIFICATE_VERIFY_FAILED`), Python doesn't trust your company's
internal CA, even though the browser does. Export the root CA certificate as Base64
`.pem`, for example from the browser's certificate viewer. Then point mrstat at it in
each section:

```toml
[gitlab]
verify_ssl = "C:/certs/corp-root-ca.pem"
[jira]
verify_ssl = "C:/certs/corp-root-ca.pem"
```

Alternatively, `pip install pip-system-certs` makes Python use the Windows certificate
store, and you don't need the file.

**Proxy:** if the internal servers are only reachable through a proxy, set
`$env:HTTPS_PROXY`. If they're reached directly while a proxy is set for everything else,
set `$env:NO_PROXY = "yourcorp.internal"`.

### 4. Test the configuration

```powershell
mrstat test
```

`mrstat test` (alias `mrstat check`) checks each service step by step and shows, for every
failure, what kind of error it is and what to do about it:

```
GitLab  https://gitlab.yourcorp.internal
  [ OK ] server reachable: GitLab 17.3.1
  [ OK ] authentication: as your.login (Your Name)
  [ OK ] token: scopes: read_api, expires 2027-03-01 (97 days)
  [ OK ] project group/subgroup/project: id 1234, merge requests readable (some)

Jira  https://jira.yourcorp.internal
  [ OK ] server reachable: Jira 9.12.0 (Server)
  [FAIL] authentication: authentication failed (HTTP 401)
         -> Token or password wrong/expired: create a personal access token ...

LLM  https://llm-gateway.yourcorp.internal/v1
  [WARN] server reachable (/models): model my-model not in the list (may still work); available: ...
  [ OK ] test completion (a few tokens): model my-model answered in 1.2s (31 in / 5 out tokens)

Summary: GitLab OK, Jira FAILED, LLM OK with warnings
```

What is checked:

| Service | Steps |
|---|---|
| GitLab | server reachable (`/version`), authentication (`/user`), token scopes and expiry, each configured project (and that its MRs are readable), each group |
| Jira | server reachable (`/serverInfo`), authentication (`/myself`), visible projects vs. `project_keys`, whether `epic_link_field` / `story_points_field` exist (and suggests candidates), whether your `child_relations` / `parent_relations` phrases exist as link types (and lists the available ones) |
| LLM | `/models` (optional, many gateways don't offer it), one tiny test completion with the configured `response_format`, and (automatic for Ollama, else `--context-check`) a long prompt that detects silent truncation |

Recognised error types include: host name not resolvable (DNS, not on VPN), connection
refused, timeout, untrusted TLS certificate (internal CA), proxy errors, an HTML/SSO login
page instead of the API, and HTTP 401 / 403 / 404 / 407 / 429 / 5xx. Requests are not
retried during the test, so you see the first error right away.

Options:

```powershell
mrstat test --only jira          # gitlab, jira, llm (comma separated)
mrstat test --no-llm-call        # don't send any test completion
mrstat test --context-check      # also send a long prompt to detect truncation (auto for Ollama)
mrstat test --timeout 60         # seconds per request (default 20)
mrstat test --json               # machine-readable output
```

The exit code is 1 if any service failed. The output contains no tokens, so you can paste
it when asking for help.

### 5. LLM endpoint (optional)

```toml
[llm]
base_url = "https://llm-gateway.yourcorp.internal/v1"   # "/chat/completions" is appended
model = "your-model-name"
```

```powershell
$env:MRSTAT_LLM_API_KEY = "..."
```

Common variants:

| Endpoint | Settings |
|---|---|
| OpenAI-compatible gateway, LiteLLM, vLLM | `base_url = "https://host/v1"` |
| Ollama | `base_url = "http://host:11434/v1"`, no key |
| Azure OpenAI | `base_url = "https://<resource>.openai.azure.com/openai/deployments/<deployment>"`, `api_key_header = "api-key"`, `extra_query = { "api-version" = "2024-10-21" }` |
| Newer OpenAI reasoning models | `max_tokens_param = "max_completion_tokens"`, `temperature = "omit"` |
| Server rejects `response_format` | `response_format = "json_object"` or `"none"` |

`mrstat test --only llm` checks the endpoint with one tiny completion.

#### Local models with Ollama (e.g. gemma3:12b)

```toml
[llm]
base_url = "http://localhost:11434/v1"
model = "gemma3:12b"
response_format = "json_schema"   # Ollama supports structured output
max_input_chars = 40000           # fits a 16k-token context with room for the answer
chunk_chars = 30000
```

**Context window:** Ollama runs models with a small context window by default and cuts
longer prompts *silently*. The OpenAI-compatible endpoint can't change this per request,
so set it on the Ollama side, either for the server:

```powershell
$env:OLLAMA_CONTEXT_LENGTH = "16384"; ollama serve
```

or as a model variant (`Modelfile` with `FROM gemma3:12b` and `PARAMETER num_ctx 16384`,
then `ollama create gemma3-16k -f Modelfile` and `model = "gemma3-16k"`).

`mrstat test` detects Ollama URLs and then also sends one long prompt of
`max_input_chars` characters with a code word at the start and at the end. If the start
is lost, the check fails with the hint above. For other endpoints, run it with
`mrstat test --context-check`. A larger context needs more GPU memory. If that's not
available, lower `max_input_chars` and `chunk_chars` instead.

## Analyze single MRs and issues

```powershell
mrstat -v mr https://gitlab.yourcorp.internal/group/project/-/merge_requests/1234 --tree
mrstat mr group/project!1234 --json
mrstat mr group/project!1234 --no-expand     # only this MR, not the whole ticket cluster
mrstat issue ABC-4711

# LLM estimate
mrstat estimate group/project!1234 --dry-run --dump-prompt prompt.txt   # sends nothing
mrstat estimate group/project!1234
```

## Statistics database

```bash
mrstat sync --since 2026-01-01 --until 2026-07-01   # historical (MRs merged in the span)
mrstat sync --incremental                           # from the last successful sync
mrstat sync --incremental --llm                     # ... and LLM-estimate the synced MRs
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
`MRSTAT_SYNC_EVERY` seconds (first run from `MRSTAT_INITIAL_SINCE`). To also run LLM
estimates during the sync, add `--llm` to the `serve` command. Data lives in the
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
