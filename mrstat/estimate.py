"""LLM-based effort estimate of a merge request from its combined (final) diff.

The model is deliberately "blind": it gets the task description (MR + referenced tickets'
summary/description), a file overview and the filtered combined diff - but no Jira
estimates, story points, booked time or commit-time effort, so it cannot anchor on them.
It estimates the effort the *final change* represents; trial and error that left no trace
in the result is intentionally not part of it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
from dataclasses import dataclass, field

from mrstat import db as dbm
from mrstat.config import Config
from mrstat.db import Database
from mrstat.llm_client import LLMClient, LLMError, parse_json_object
from mrstat.timeutil import iso, utcnow

log = logging.getLogger(__name__)

PROMPT_VERSION = "v1"

BREAKDOWN_KEYS = ["implementation", "tests", "refactoring", "config_infra", "other"]

ESTIMATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hours_low", "hours_likely", "hours_high", "breakdown",
                 "complexity_factors", "confidence", "rationale"],
    "properties": {
        "hours_low": {"type": "number"},
        "hours_likely": {"type": "number"},
        "hours_high": {"type": "number"},
        "breakdown": {
            "type": "object", "additionalProperties": False, "required": BREAKDOWN_KEYS,
            "properties": {k: {"type": "number"} for k in BREAKDOWN_KEYS},
        },
        "complexity_factors": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "rationale": {"type": "string"},
    },
}

SYSTEM_PROMPT = """\
You estimate software development effort for merge requests in an enterprise software product.

You receive the task description (merge request text and the linked tickets), an overview of \
the changed files, and the combined diff of the final change (target branch -> merged result). \
Some files are listed but not shown (generated, lock files, binaries, deleted files); treat \
them as near-zero effort unless the task clearly required hand-written work there.

Estimate the hands-on effort in hours that an experienced developer who already knows this \
codebase would need to produce exactly this final change: understanding the task, writing \
and adjusting code and tests, local debugging, and addressing typical review feedback. \
Exclude meetings, waiting time, coordination, and dead ends that left no trace in the result.

Guidance:
- Lines changed are not effort. Mechanical changes (renames, moved code, formatting, \
boilerplate, generated-looking code, simple config) are cheap. Subtle logic, concurrency, \
data or schema migrations, security-relevant code, public API changes, cross-cutting \
changes and tricky test setups are expensive.
- Give a realistic range: hours_low <= hours_likely <= hours_high.
- breakdown splits hours_likely into implementation, tests, refactoring, config_infra and \
other; its values must sum to hours_likely.
- confidence reflects how well the diff and description let you judge the effort.
- rationale: 2-5 sentences naming the main effort drivers.

Answer with a single JSON object and nothing else, exactly in this form:
{"hours_low": 2.0, "hours_likely": 4.0, "hours_high": 8.0,
 "breakdown": {"implementation": 2.5, "tests": 1.0, "refactoring": 0.5, "config_infra": 0.0, "other": 0.0},
 "complexity_factors": ["..."], "confidence": "medium", "rationale": "..."}"""

SUMMARY_PROMPT = """\
You summarize part of a large merge request so that someone else can later estimate the \
development effort without seeing the code. For each file or group of related files in the \
diff below, describe concisely what changed and how: new logic vs. mechanical edits, \
algorithms, data structures, error handling, tests, migrations, notable difficulty. \
Do not estimate hours. Plain text, at most about 400 words."""


@dataclass
class FileEntry:
    path: str
    added: int
    deleted: int
    status: str  # added | deleted | renamed | modified
    diff: str
    excluded: str | None = None  # reason, or None if the diff is sent


@dataclass
class PreparedInput:
    project_id: int
    iid: int
    header: str  # MR + tickets + file overview
    files: list[FileEntry]
    strategy: str  # full | summarize | truncate | skip
    chunks: list[str] = field(default_factory=list)  # diff chunks (summarize/truncate)
    notes: list[str] = field(default_factory=list)

    @property
    def included(self) -> list[FileEntry]:
        return [f for f in self.files if not f.excluded]

    @property
    def diff_chars(self) -> int:
        return sum(len(_file_block(f)) for f in self.included)

    def input_hash(self, model: str) -> str:
        h = hashlib.sha256()
        for part in (PROMPT_VERSION, model, self.strategy, self.header,
                     *(_file_block(f) for f in self.included)):
            h.update(part.encode("utf-8", "replace"))
            h.update(b"\0")
        return h.hexdigest()

    def final_messages(self, summaries: list[str] | None = None) -> list[dict]:
        if self.strategy == "summarize":
            if summaries is None:
                summaries = [f"[summary of diff part {i + 1} - produced by the model at run time]"
                             for i in range(len(self.chunks))]
            body = ("# Change summaries\nThe diff was too large to include; it was summarized "
                    "per file group:\n\n" + "\n\n".join(
                        f"## Part {i + 1}\n{s}" for i, s in enumerate(summaries)))
        elif self.strategy == "truncate":
            body = "# Diff (truncated - see notes in the file overview)\n" + "".join(self.chunks)
        else:
            body = "# Diff\n" + "".join(_file_block(f) for f in self.included)
        return [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"{self.header}\n{body}"}]

    def summary_messages(self, i: int) -> list[dict]:
        return [{"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": f"{self.header}\n# Diff part {i + 1} of {len(self.chunks)}\n"
                                            f"{self.chunks[i]}"}]

    @property
    def planned_calls(self) -> int:
        return {"skip": 0, "summarize": len(self.chunks) + 1}.get(self.strategy, 1)


def _file_block(f: FileEntry) -> str:
    return f"\n--- {f.path} ({f.status}, +{f.added} -{f.deleted})\n{f.diff.rstrip()}\n"


def _count_lines(diff: str) -> tuple[int, int]:
    added = deleted = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            deleted += 1
    return added, deleted


def _excluded_by(path: str, patterns: list[str]) -> str | None:
    for pat in patterns:
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path, "*/" + pat):
            return f"matches {pat}"
    return None


def classify_files(raw_diffs: list[dict], cfg: Config) -> list[FileEntry]:
    lc = cfg.llm
    out = []
    for d in raw_diffs:
        path = d.get("new_path") or d.get("old_path") or "?"
        diff = d.get("diff") or ""
        added, deleted = _count_lines(diff)
        if d.get("new_file"):
            status = "added"
        elif d.get("deleted_file"):
            status = "deleted"
        elif d.get("renamed_file"):
            status = f"renamed from {d.get('old_path')}"
        else:
            status = "modified"
        reason = _excluded_by(path, lc.exclude)
        if not reason and d.get("generated_file"):
            reason = "generated (GitLab)"
        if not reason and (d.get("too_large") or d.get("collapsed")) and not diff.strip():
            reason = "diff not provided by GitLab (too large)"
        if not reason and not diff.strip():
            reason = "no textual diff (binary, mode change or pure rename)"
        if not reason and diff.lstrip().startswith("Binary files"):
            reason = "binary"
        if not reason and status == "deleted" and not lc.include_deleted_content:
            reason = "deleted file (content not sent)"
        out.append(FileEntry(path, added, deleted, status, diff, reason))
    return out


def _clip(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... description truncated, {len(text) - limit} more characters]"


def build_header(mr: dict, tickets: list[dict], files: list[FileEntry], cfg: Config) -> str:
    lim = cfg.llm.ticket_description_chars
    lines = ["# Merge request",
             f"Title: {mr.get('title') or ''}",
             f"Branches: {mr.get('source_branch')} -> {mr.get('target_branch')}",
             "Description:", _clip(mr.get("description"), lim * 2) or "(none)", ""]
    if tickets:
        lines.append("# Linked tickets")
        for t in tickets:
            lines.append(f"## {t['key']} ({t.get('issue_type') or 'ticket'}): {t.get('summary') or ''}")
            if t.get("description"):
                lines.append(_clip(t["description"], lim))
            lines.append("")
    total_a = sum(f.added for f in files)
    total_d = sum(f.deleted for f in files)
    lines.append(f"# Changed files ({len(files)} files, +{total_a} -{total_d} lines)")
    for f in files:
        mark = f"  [not shown: {f.excluded}]" if f.excluded else ""
        lines.append(f"{f.path}  ({f.status}, +{f.added} -{f.deleted}){mark}")
    return "\n".join(lines) + "\n"


def _chunk(files: list[FileEntry], size: int) -> tuple[list[str], list[str]]:
    """Group file blocks into chunks of at most `size` chars; oversize files are cut with a
    visible marker. Returns (chunks, notes)."""
    chunks, notes, cur = [], [], ""
    for f in files:
        block = _file_block(f)
        if len(block) > size:
            keep = max(size - 200, 1000)
            notes.append(f"{f.path}: diff truncated to {keep} of {len(block)} characters")
            block = block[:keep] + f"\n[... diff of {f.path} truncated: {len(block) - keep} more characters]\n"
        if cur and len(cur) + len(block) > size:
            chunks.append(cur)
            cur = ""
        cur += block
    if cur:
        chunks.append(cur)
    return chunks, notes


class Estimator:
    def __init__(self, cfg: Config, db: Database, gitlab, llm: LLMClient | None = None):
        self.cfg, self.db, self.gl, self.llm = cfg, db, gitlab, llm

    def prepare(self, project_id: int, iid: int) -> PreparedInput:
        with self.db.connect() as conn:
            mr = conn.execute("SELECT * FROM mrs WHERE project_id=? AND iid=?", (project_id, iid)).fetchone()
            if not mr:
                raise ValueError(f"MR {project_id}!{iid} is not in the database - analyze it first")
            tickets = [dict(r) for r in conn.execute(
                "SELECT i.key, i.summary, i.description, i.issue_type FROM mr_issues mi "
                "JOIN issues i ON i.key=mi.issue_key WHERE mi.project_id=? AND mi.iid=? AND i.found=1 "
                "ORDER BY i.key", (project_id, iid))]
        files = classify_files(self.gl.mr_diffs(project_id, iid), self.cfg)
        header = build_header(dict(mr), tickets, files, self.cfg)
        prep = PreparedInput(project_id, iid, header, files, "full")

        lc = self.cfg.llm
        total = len(header) + prep.diff_chars
        if total > lc.max_input_chars:
            strategy = lc.large_mr_strategy
            prep.strategy = strategy
            if strategy == "summarize":
                prep.chunks, prep.notes = _chunk(prep.included, lc.chunk_chars)
                prep.notes.insert(0, f"input {total} chars > max_input_chars {lc.max_input_chars}: "
                                     f"diff summarized in {len(prep.chunks)} part(s)")
            elif strategy == "truncate":
                chunks, notes = _chunk(prep.included, max(lc.max_input_chars - len(header), 10_000))
                prep.chunks = chunks[:1]
                prep.notes = [f"input {total} chars > max_input_chars: only the first "
                              f"{len(prep.chunks[0]) if prep.chunks else 0} diff characters are sent", *notes]
            else:
                prep.strategy = "skip"
                prep.notes = [f"input {total} chars > max_input_chars {lc.max_input_chars}: skipped"]
        return prep

    def cached(self, prep: PreparedInput):
        with self.db.connect() as conn:
            return conn.execute(
                "SELECT * FROM llm_estimates WHERE project_id=? AND iid=? AND input_hash=?",
                (prep.project_id, prep.iid, prep.input_hash(self.llm.model))).fetchone()

    def estimate(self, project_id: int, iid: int, *, force: bool = False) -> dict | None:
        """Estimate and store. Returns the stored row (dict), or None if skipped."""
        if self.llm is None:
            raise ValueError("no LLM configured")
        prep = self.prepare(project_id, iid)
        if prep.strategy == "skip":
            log.warning("%s!%s: %s", project_id, iid, "; ".join(prep.notes))
            return None
        if not force:
            hit = self.cached(prep)
            if hit:
                log.info("%s!%s: estimate unchanged (cached)", project_id, iid)
                return dict(hit)

        p_tok = c_tok = calls = 0
        summaries = None
        if prep.strategy == "summarize":
            summaries = []
            for i in range(len(prep.chunks)):
                res = self.llm.chat(prep.summary_messages(i))
                summaries.append(res.text.strip())
                p_tok, c_tok, calls = p_tok + res.prompt_tokens, c_tok + res.completion_tokens, calls + 1

        messages = prep.final_messages(summaries)
        data = None
        for attempt in range(2):
            res = self.llm.chat(messages, json_schema=ESTIMATE_SCHEMA, schema_name="effort_estimate")
            p_tok, c_tok, calls = p_tok + res.prompt_tokens, c_tok + res.completion_tokens, calls + 1
            try:
                data = validate_estimate(parse_json_object(res.text))
                break
            except LLMError as exc:
                if attempt:
                    raise
                log.warning("%s!%s: invalid estimate (%s), retrying once", project_id, iid, exc)
                messages = messages + [
                    {"role": "assistant", "content": res.text},
                    {"role": "user", "content": f"That was not valid: {exc}. Reply with only the JSON object."}]

        row = {
            "project_id": project_id, "iid": iid, "model": self.llm.model,
            "prompt_version": PROMPT_VERSION, "input_hash": prep.input_hash(self.llm.model),
            "created_at": iso(utcnow()),
            "hours_low": data["hours_low"], "hours_likely": data["hours_likely"],
            "hours_high": data["hours_high"], "confidence": data["confidence"],
            "breakdown": json.dumps(data["breakdown"]),
            "complexity": json.dumps(data["complexity_factors"]),
            "rationale": data["rationale"], "strategy": prep.strategy,
            "input_chars": sum(len(m["content"]) for m in messages),
            "files_included": len(prep.included),
            "files_excluded": len(prep.files) - len(prep.included),
            "prompt_tokens": p_tok, "completion_tokens": c_tok, "calls": calls,
        }
        with self.db.connect() as conn:
            dbm.insert_llm_estimate(conn, row)
        return row


def validate_estimate(d: dict) -> dict:
    try:
        lo, likely, hi = (float(d["hours_low"]), float(d["hours_likely"]), float(d["hours_high"]))
        bd = {k: float((d.get("breakdown") or {}).get(k, 0) or 0) for k in BREAKDOWN_KEYS}
    except (KeyError, TypeError, ValueError) as exc:
        raise LLMError(f"missing or non-numeric field: {exc}") from exc
    if min(lo, likely, hi) < 0:
        raise LLMError("negative hours")
    lo, likely, hi = sorted((lo, likely, hi))
    conf = str(d.get("confidence", "low")).lower()
    return {
        "hours_low": lo, "hours_likely": likely, "hours_high": hi, "breakdown": bd,
        "complexity_factors": [str(x) for x in (d.get("complexity_factors") or [])],
        "confidence": conf if conf in ("low", "medium", "high") else "low",
        "rationale": str(d.get("rationale", "")),
    }


def format_dry_run(prep: PreparedInput) -> str:
    msgs = prep.final_messages()
    chars = sum(len(m["content"]) for m in msgs) + sum(len(c) for c in prep.chunks if prep.strategy == "summarize")
    out = [f"Dry run for {prep.project_id}!{prep.iid} - nothing is sent.",
           f"  files: {len(prep.files)} total, {len(prep.included)} with diff sent, "
           f"{len(prep.files) - len(prep.included)} listed only",
           f"  strategy: {prep.strategy}, LLM calls: {prep.planned_calls}",
           f"  input: {chars} characters (~{chars // 4} tokens, rough estimate at 4 chars/token)"]
    out += [f"  note: {n}" for n in prep.notes]
    out.append("")
    out.append("  Files not sent:")
    for f in prep.files:
        if f.excluded:
            out.append(f"    {f.path}: {f.excluded}")
    return "\n".join(out)


def format_estimate(row: dict | None, hours_per_day: float) -> str:
    if not row:
        return "  LLM estimate: skipped"
    bd = json.loads(row["breakdown"] or "{}")
    cf = json.loads(row["complexity"] or "[]")
    return "\n".join([
        f"  LLM estimate ({row['model']}, prompt {row['prompt_version']}, {row['strategy']}):",
        f"    {row['hours_low']:.1f} / {row['hours_likely']:.1f} / {row['hours_high']:.1f} h "
        f"(low / likely / high) = {row['hours_likely'] / hours_per_day:.2f} person days, "
        f"confidence {row['confidence']}",
        "    breakdown: " + ", ".join(f"{k} {v:.1f}" for k, v in bd.items()),
        "    factors: " + ("; ".join(cf) or "-"),
        f"    rationale: {row['rationale']}",
        f"    tokens: {row['prompt_tokens']} in / {row['completion_tokens']} out, {row['calls']} call(s)",
    ])
