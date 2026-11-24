# AGENTS.md

Instructions for coding agents working on this repository.

The project reference is [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): requirements,
decisions agreed with the user, architecture, data model, conventions, testing approach
and roadmap. Read it before making changes, and update it when you change any of these.
User-facing documentation is [README.md](README.md).

Essentials:

* Python ≥ 3.11. Install with `pip install -e '.[web,dev]'`, test with `pytest -q`.
* Tests must not need network access to real GitLab, Jira or LLM services. Use
  `tests/fakes.py` and the local stub server in `tests/test_selftest.py`.
* Never put tokens or API keys in files, output or commits.
* Bump `PROMPT_VERSION` in `mrstat/estimate.py` whenever the LLM input or prompt changes.
* Database changes go through `SCHEMA` / `MIGRATIONS` in `mrstat/db.py`. Existing user
  databases must keep working.
