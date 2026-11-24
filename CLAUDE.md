# CLAUDE.md

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first. It holds the requirements, the
decisions agreed with the user, the module structure, the data model, conventions and the
roadmap. Keep it up to date when you change any of these. User-facing usage is in
[README.md](README.md).

Quick facts:

* Setup and tests: `python -m venv .venv && .venv/bin/pip install -e '.[web,dev]' && .venv/bin/pytest -q`
* This machine has no access to the user's GitLab, Jira or LLM. Never try to reach them.
  Test with the fakes in `tests/fakes.py` and the stub server in `tests/test_selftest.py`.
  The user tests on a Windows 11 notebook and pastes the output back.
* Bump `PROMPT_VERSION` in `mrstat/estimate.py` whenever what is sent to the LLM changes.
* Schema changes go through `SCHEMA` / `MIGRATIONS` in `mrstat/db.py`. Never drop or rename columns.
* New settings: dataclass in `mrstat/config.py`, plus `mrstat.example.toml` and README.
* The user pulls from `origin/master` (https://github.com/tweggen/mrstat). Push after
  finishing a change.
