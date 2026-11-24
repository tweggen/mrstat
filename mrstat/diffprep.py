"""Deterministic diff cleanup and per-file facts, so the model sees the same change the same
way every time and doesn't spend attention on noise.

Per MR (all files together):
* whitespace-only changes: a removed and an added line in the same hunk that differ only in
  whitespace are dropped (also added/removed blank lines)
* moved code: runs of >= `min_move_block` substantial lines (>= 8 non-blank characters)
  that were removed in one place and added elsewhere (other file, or other part of the same
  file; re-indentation allowed) are replaced by one marker "~ [N lines moved to/from ...]"
* comment-only and import/include lines are dropped (language by file extension)
* context is reduced to `context_lines` lines around the remaining changes; hunks without
  remaining changes disappear

Everything removed is counted, so the facts table still shows the full picture.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

# ----------------------------------------------------------------------- languages
_LANG = {
    "py": "python", "pyi": "python", "sh": "shell", "bash": "shell", "ps1": "powershell",
    "rb": "ruby", "pl": "perl", "pm": "perl", "r": "r", "yaml": "yaml", "yml": "yaml",
    "toml": "toml", "cmake": "cmake",
    "c": "c", "h": "c", "cc": "cpp", "cpp": "cpp", "cxx": "cpp", "hpp": "cpp", "hh": "cpp",
    "java": "java", "kt": "kotlin", "kts": "kotlin", "scala": "scala", "groovy": "groovy",
    "gradle": "groovy", "js": "javascript", "jsx": "javascript", "mjs": "javascript",
    "cjs": "javascript", "ts": "typescript", "tsx": "typescript", "cs": "csharp",
    "go": "go", "rs": "rust", "swift": "swift", "php": "php", "dart": "dart", "m": "objc",
    "sql": "sql", "lua": "lua", "hs": "haskell",
    "xml": "xml", "html": "html", "htm": "html", "xaml": "xml", "csproj": "xml", "vue": "html",
}
_HASH = {"python", "shell", "powershell", "ruby", "perl", "r", "yaml", "toml", "cmake"}
_SLASH = {"c", "cpp", "java", "kotlin", "scala", "groovy", "javascript", "typescript", "csharp",
          "go", "rust", "swift", "php", "dart", "objc"}
_DASH = {"sql", "lua", "haskell"}
_IMPORT = {
    "python": re.compile(r"^\s*(import\s+\S|from\s+\S+\s+import\s)"),
    "java": re.compile(r"^\s*import\s+[\w.*]+\s*;"),
    "kotlin": re.compile(r"^\s*import\s+[\w.*`]+"),
    "scala": re.compile(r"^\s*import\s+\S"),
    "groovy": re.compile(r"^\s*import\s+\S"),
    "javascript": re.compile(r"^\s*(import\s.+from\s|import\s+['\"]|(const|let|var)\s+\w+\s*=\s*require\()"),
    "typescript": re.compile(r"^\s*(import\s.+from\s|import\s+['\"]|export\s.+from\s)"),
    "csharp": re.compile(r"^\s*(global\s+)?using\s+[\w.=\s]+;"),
    "c": re.compile(r"^\s*#\s*include\s"),
    "cpp": re.compile(r"^\s*#\s*include\s"),
    "objc": re.compile(r"^\s*#\s*(include|import)\s"),
    "go": re.compile(r'^\s*(import\s+("|\()|"[\w./-]+"\s*$|\w+\s+"[\w./-]+"\s*$)'),
    "rust": re.compile(r"^\s*(pub\s+)?use\s+\S"),
    "php": re.compile(r"^\s*use\s+[\w\\]+"),
    "dart": re.compile(r"^\s*import\s+'"),
    "swift": re.compile(r"^\s*import\s+\w"),
}

_TEST = re.compile(r"(^|/)(tests?|__tests__|spec|testing)/|(^|/)src/test/|_test\.\w+$|\.test\.\w+$|"
                   r"\.spec\.\w+$|(^|/)test_[^/]+$", re.I)
_TEST_CAMEL = re.compile(r"[a-z0-9](Tests?|IT|Spec)\.\w+$")  # FooTest.java, FooTests.cs - case-sensitive
_DOCS_EXT = {"md", "rst", "txt", "adoc", "asciidoc"}
_CONFIG_EXT = {"yml", "yaml", "json", "toml", "ini", "cfg", "conf", "properties", "xml", "gradle",
               "env", "tf", "tfvars", "csproj", "sln", "props", "targets", "editorconfig"}
_CONFIG_NAMES = {"dockerfile", "makefile", "jenkinsfile", "pom.xml", ".gitlab-ci.yml",
                 "cmakelists.txt", ".gitignore", "docker-compose.yml"}


def language(path: str) -> str | None:
    name = PurePosixPath(path).name.lower()
    if name == "dockerfile" or name.startswith("dockerfile."):
        return "shell"
    if name in ("cmakelists.txt",):
        return "cmake"
    return _LANG.get(name.rsplit(".", 1)[-1]) if "." in name else None


def category(path: str) -> str:
    name = PurePosixPath(path).name.lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if name in _CONFIG_NAMES or name.startswith("dockerfile"):
        return "config"
    if _TEST.search(path) or _TEST_CAMEL.search(path):
        return "test"
    if ext in _DOCS_EXT or path.lower().startswith(("doc/", "docs/")):
        return "docs"
    if ext in _CONFIG_EXT:
        return "config"
    if language(path):
        return "code"
    return "other"


def is_comment(text: str, lang: str | None) -> bool:
    s = text.strip()
    if not s or lang is None:
        return False
    if lang in _HASH and s.startswith("#"):
        return True
    if lang in _SLASH and (s.startswith(("//", "/*", "*/", "* ")) or s == "*"):
        return True  # "* " / "*" = inside a block comment; "*ptr = 1;" is code
    if lang in _DASH and s.startswith("--"):
        return True
    if lang in ("xml", "html") and s.startswith("<!--") and s.endswith("-->"):
        return True
    return False


def is_import(text: str, lang: str | None) -> bool:
    rx = _IMPORT.get(lang or "")
    return bool(rx and rx.match(text))


# ----------------------------------------------------------------------- model
@dataclass
class Line:
    kind: str  # " ", "+", "-"
    text: str
    keep: bool = True
    reason: str | None = None  # whitespace | moved | comment | import
    marker: str | None = None  # shown instead of this (dropped) line, e.g. a moved-block note

    @property
    def visible(self) -> bool:
        return self.keep or self.marker is not None


@dataclass
class Hunk:
    section: str  # function/section context from the @@ header
    lines: list[Line]


@dataclass
class FileFacts:
    path: str
    category: str
    language: str | None
    status: str
    raw_added: int = 0
    raw_deleted: int = 0
    added: int = 0  # still shown after cleanup
    deleted: int = 0
    moved: int = 0  # changed lines recognised as moved (both sides)
    whitespace: int = 0
    comments: int = 0
    imports: int = 0
    hunks: int = 0  # hunks with remaining changes
    functions: list[str] = field(default_factory=list)


@dataclass
class CleanFile:
    facts: FileFacts
    hunks: list[Hunk]

    def render(self) -> str:
        out = []
        for h in self.hunks:
            out.append(f"@@ {h.section}".rstrip())
            for ln in h.lines:
                if ln.marker:
                    out.append(ln.marker)
                elif ln.keep:
                    out.append(f"{ln.kind}{ln.text}")
        return "\n".join(out) + ("\n" if out else "")


def norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


_HUNK = re.compile(r"^@@ [^@]* @@ ?(.*)$")


def parse_hunks(diff: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    for raw in diff.splitlines():
        if raw.startswith("@@"):
            m = _HUNK.match(raw)
            hunks.append(Hunk((m.group(1) if m else "").strip(), []))
            continue
        if not hunks or raw.startswith("\\"):  # file headers before the first hunk, "\ No newline"
            continue
        if raw.startswith(("+++", "---")) and not hunks[-1].lines:
            continue
        kind = raw[:1] if raw[:1] in "+- " else " "
        hunks[-1].lines.append(Line(kind, raw[1:] if raw[:1] in "+- " else raw))
    return hunks


# ----------------------------------------------------------------------- cleanup
@dataclass
class CleanOptions:
    whitespace: bool = True
    moves: bool = True
    min_move_block: int = 3
    comments: bool = True
    imports: bool = True
    context_lines: int = 1


def _mark_whitespace(hunk: Hunk) -> None:
    removed: dict[str, list[Line]] = defaultdict(list)
    for ln in hunk.lines:
        if ln.kind == "-" and ln.keep:
            removed[norm(ln.text)].append(ln)
    for ln in hunk.lines:
        if ln.kind != "+" or not ln.keep:
            continue
        key = norm(ln.text)
        if removed.get(key):
            other = removed[key].pop()
            ln.keep = other.keep = False
            ln.reason = other.reason = "whitespace"
    for ln in hunk.lines:  # added/removed blank lines
        if ln.kind in "+-" and ln.keep and not ln.text.strip():
            ln.keep, ln.reason = False, "whitespace"


def _runs(hunk: Hunk, kind: str):
    """Maximal runs of kept lines of `kind` (other kinds in between end a run, except the
    opposite change kind, which is common in replaced blocks)."""
    run: list[int] = []
    for i, ln in enumerate(hunk.lines):
        if ln.kind == kind and ln.keep:
            run.append(i)
        elif ln.kind == " " and run:
            yield run
            run = []
    if run:
        yield run


def _mark_moves(files: list[tuple[str, list[Hunk]]], min_block: int) -> None:
    index = {"+": defaultdict(Counter), "-": defaultdict(Counter)}  # norm -> Counter(path)
    for path, hunks in files:
        for h in hunks:
            for ln in h.lines:
                if ln.kind in "+-" and ln.keep and norm(ln.text):
                    index[ln.kind][norm(ln.text)][path] += 1

    for path, hunks in files:
        for h in hunks:
            for kind, other in (("-", "+"), ("+", "-")):
                for run in list(_runs(h, kind)):
                    # split the run into stretches of lines that exist on the other side
                    stretch: list[int] = []
                    for i in run + [None]:
                        hit = i is not None and norm(h.lines[i].text) in index[other]
                        if hit:
                            stretch.append(i)
                            continue
                        if stretch:
                            _apply_move(path, h, stretch, kind, index[other], min_block)
                        stretch = []


def _apply_move(path, hunk, stretch, kind, other_index, min_block) -> None:
    substantial = [i for i in stretch if len(norm(hunk.lines[i].text)) >= 8]
    if len(substantial) < min_block:
        return
    where = Counter()
    for i in stretch:
        where.update(other_index[norm(hunk.lines[i].text)])
    target = where.most_common(1)[0][0]
    target = "elsewhere in this file" if target == path else target
    for i in stretch:
        hunk.lines[i].keep, hunk.lines[i].reason = False, "moved"
    verb = "moved to" if kind == "-" else "moved from"
    hunk.lines[stretch[0]].marker = f"~ [{len(stretch)} lines {verb} {target}]"


def _trim_context(hunk: Hunk, n: int) -> None:
    # distances count only lines that remain visible (dropped noise lines collapse)
    seq = [ln for ln in hunk.lines if ln.kind == " " or ln.visible]
    anchors = [i for i, ln in enumerate(seq) if ln.kind in "+-"]
    for i, ln in enumerate(seq):
        if ln.kind == " ":
            ln.keep = any(abs(i - a) <= n for a in anchors)


def clean(entries: list[tuple[str, str, str]], opts: CleanOptions) -> list[CleanFile]:
    """entries: (path, status, unified diff text). Returns cleaned files in the same order."""
    parsed = [(path, status, parse_hunks(diff)) for path, status, diff in entries]
    facts = []
    for path, status, hunks in parsed:
        lang = language(path)
        f = FileFacts(path, category(path), lang, status)
        for h in hunks:
            for ln in h.lines:
                if ln.kind == "+":
                    f.raw_added += 1
                elif ln.kind == "-":
                    f.raw_deleted += 1
            if opts.whitespace:
                _mark_whitespace(h)
            for ln in h.lines:
                if ln.kind in "+-" and ln.keep:
                    if opts.comments and is_comment(ln.text, lang):
                        ln.keep, ln.reason = False, "comment"
                    elif opts.imports and is_import(ln.text, lang):
                        ln.keep, ln.reason = False, "import"
        facts.append(f)

    if opts.moves:
        _mark_moves([(p, h) for p, _, h in parsed], opts.min_move_block)

    counter = {"whitespace": "whitespace", "moved": "moved", "comment": "comments", "import": "imports"}
    out = []
    for f, (path, status, hunks) in zip(facts, parsed):
        kept_hunks = []
        for h in hunks:
            for ln in h.lines:
                if ln.kind in "+-" and not ln.keep:
                    attr = counter[ln.reason]
                    setattr(f, attr, getattr(f, attr) + 1)
                elif ln.kind == "+":
                    f.added += 1
                elif ln.kind == "-":
                    f.deleted += 1
            if any(ln.kind in "+-" and ln.visible for ln in h.lines):
                _trim_context(h, opts.context_lines)
                kept_hunks.append(h)
                if h.section and h.section not in f.functions:
                    f.functions.append(h.section)
        f.hunks = len(kept_hunks)
        out.append(CleanFile(f, kept_hunks))
    return out


# ----------------------------------------------------------------------- facts table
def facts_summary(files: list[FileFacts]) -> str:
    by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for f in files:
        by_cat[f.category][0] += f.raw_added
        by_cat[f.category][1] += f.raw_deleted
    moved = sum(f.moved for f in files)
    dirs = {PurePosixPath(f.path).parts[0] if len(PurePosixPath(f.path).parts) > 1 else "." for f in files}
    cats = ", ".join(f"{c} +{a} -{d}" for c, (a, d) in sorted(by_cat.items()))
    return (f"{len(files)} files ({cats}); {moved} changed lines are moved code; "
            f"spread over {len(dirs)} top-level director{'y' if len(dirs) == 1 else 'ies'}")


def facts_line(f: FileFacts, excluded: str | None = None) -> str:
    noise = [f"{n} {label}" for n, label in ((f.moved, "moved"), (f.whitespace, "whitespace"),
                                             (f.comments, "comment"), (f.imports, "import")) if n]
    parts = [f"{f.path}", f"[{f.category}{', ' + f.language if f.language else ''}, {f.status}]",
             f"raw +{f.raw_added} -{f.raw_deleted}"]
    if excluded:
        parts.append(f"not shown: {excluded}")
    else:
        parts.append(f"shown +{f.added} -{f.deleted}")
        if noise:
            parts.append("removed as noise: " + ", ".join(noise))
        if f.functions:
            fn = "; ".join(f.functions[:6]) + (f" (+{len(f.functions) - 6} more)" if len(f.functions) > 6 else "")
            parts.append(f"in: {fn}")
    return " | ".join(parts)
