"""Progress reporting for long runs (sync, LLM estimates, recompute).

* ``bar``   - one status line updated in place (terminals, incl. Git Bash/mintty)
* ``lines`` - one line per item (logs, CI, `docker logs`)
* ``none``  - silent
* ``StatusProgress`` - writes into a dict (shown by the web UI during periodic syncs)
"""

from __future__ import annotations

import os
import shutil
import sys
import time


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


class Progress:
    """No-op base class; also the 'none' mode."""

    def phase(self, name: str, total: int | None = None) -> None: ...
    def advance(self, label: str = "", n: int = 1) -> None: ...
    def note(self, message: str) -> None: ...
    def done(self) -> None: ...


class _Phase:
    def __init__(self, name: str, total: int | None):
        self.name, self.total, self.count = name, total, 0
        self.start = time.monotonic()
        self.label = ""

    def text(self) -> str:
        elapsed = time.monotonic() - self.start
        if self.total:
            pct = 100 * self.count / self.total
            parts = [f"[{self.name}] {self.count}/{self.total} {pct:3.0f}%"]
            if 0 < self.count < self.total and elapsed > 2:
                eta = elapsed / self.count * (self.total - self.count)
                parts.append(f"ETA {_fmt_duration(eta)}")
            elif self.count >= self.total:
                parts.append(f"in {_fmt_duration(elapsed)}")
        else:
            parts = [f"[{self.name}] {self.count}", _fmt_duration(elapsed)]
        if self.label:
            parts.append(self.label)
        return "  ".join(parts)


class ConsoleProgress(Progress):
    def __init__(self, mode: str = "auto", stream=None):
        self.stream = stream or sys.stderr
        if mode == "auto":
            interactive = self.stream.isatty() or bool(os.environ.get("MSYSTEM"))  # Git Bash
            mode = "bar" if interactive and os.environ.get("TERM") != "dumb" else "lines"
        self.mode = mode
        self.cur: _Phase | None = None
        self._shown = 0  # length of the status line currently on screen
        self._last_draw = 0.0

    # -- drawing
    def _width(self) -> int:
        return max(40, shutil.get_terminal_size((100, 20)).columns - 1)

    def clear(self) -> None:
        if self.mode == "bar" and self._shown:
            self.stream.write("\r" + " " * self._shown + "\r")
            self.stream.flush()
            self._shown = 0

    def _draw(self, force: bool = False) -> None:
        if not self.cur:
            return
        now = time.monotonic()
        if not force and now - self._last_draw < 0.1:
            return
        self._last_draw = now
        line = self.cur.text()[: self._width()]
        pad = max(0, self._shown - len(line))
        self.stream.write("\r" + line + " " * pad)
        self.stream.flush()
        self._shown = len(line)

    def _finish_phase(self) -> None:
        if self.cur and self.mode == "bar":
            self.cur.label = ""
            self._draw(force=True)
            self.stream.write("\n")
            self.stream.flush()
            self._shown = 0
        elif self.cur and self.mode == "lines":
            self.stream.write(self.cur.text() + "\n")
            self.stream.flush()

    # -- API
    def phase(self, name: str, total: int | None = None) -> None:
        if self.mode == "none":
            return
        self._finish_phase()
        self.cur = _Phase(name, total)
        if self.mode == "bar":
            self._draw(force=True)

    def advance(self, label: str = "", n: int = 1) -> None:
        if self.mode == "none" or not self.cur:
            return
        self.cur.count += n
        self.cur.label = label
        if self.mode == "bar":
            self._draw(force=self.cur.total is not None and self.cur.count >= self.cur.total)
        elif label and self.cur.total is not None:  # lines mode: one line per counted item
            self.stream.write(self.cur.text() + "\n")
            self.stream.flush()

    def note(self, message: str) -> None:
        if self.mode == "none":
            return
        self.clear()
        self.stream.write(message + "\n")
        self.stream.flush()
        if self.mode == "bar":
            self._draw(force=True)

    def done(self) -> None:
        if self.mode == "none":
            return
        self._finish_phase()
        self.cur = None


class StatusProgress(Progress):
    """Mirror progress into a dict, e.g. the web UI's sync status."""

    def __init__(self, status: dict):
        self.status = status
        self.cur: _Phase | None = None

    def phase(self, name: str, total: int | None = None) -> None:
        self.cur = _Phase(name, total)
        self.status["progress"] = self.cur.text()

    def advance(self, label: str = "", n: int = 1) -> None:
        if self.cur:
            self.cur.count += n
            self.cur.label = label
            self.status["progress"] = self.cur.text()

    def done(self) -> None:
        self.status.pop("progress", None)
        self.cur = None


class ProgressAwareStream:
    """Stream for logging handlers: clears the status line before a log message is
    written and redraws it afterwards, so log output and the bar don't garble each other."""

    def __init__(self, progress: ConsoleProgress):
        self.progress = progress

    def write(self, s: str) -> int:
        self.progress.clear()
        n = self.progress.stream.write(s)
        if s.endswith("\n"):
            self.progress._draw(force=True)
        return n

    def flush(self) -> None:
        self.progress.stream.flush()
