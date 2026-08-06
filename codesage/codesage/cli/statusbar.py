"""Bottom status bar for the interactive REPL (CC-style runtime status).

Layout, built on an ANSI scroll region so the status bar stays pinned while
agent messages scroll above it:

    [scroll region: lines 1 .. rows-2]   agent messages scroll here
    line rows-1:                         input prompt line (fixed)
    line rows:                           status bar (fixed)

The bar shows branch | model | thinking | session | ctx meter — the ctx
meter is the compaction effect display: it re-estimates the active message
list on every redraw, so a compaction visibly drops the bar back.

Everything is emitted through the caller-provided stream; the class is inert
(no ANSI) when the stream is not a tty — the single-shot path never enables
it. Zero dependencies beyond the stdlib; Windows 10+ terminals accept VT.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from typing import TextIO

from ..engine.tokens import DEFAULT_CONTEXT_WINDOW, estimate_context_tokens
from .render import CYAN, RED, USE_COLOR, YELLOW, _c

#: ctx usage above this fraction renders as CRITICAL (red).
CRITICAL_FRACTION = 0.9
#: ctx usage above this fraction renders as a plain warning (yellow).
WARN_FRACTION = 0.8


def _git_branch(cwd: str | None = None) -> str:
    """One-shot branch probe at startup; '-' when not a git repo."""
    try:
        out = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd,
        )
        return out.stdout.strip() or "-"
    except (OSError, subprocess.TimeoutExpired):
        return "-"


class StatusBar:
    """Pinned bottom bar: scroll-region setup + redraws on demand.

    The caller drives redraws (after each rendered message/event) — there is
    no background timer, so nothing races the input-capture thread.
    """

    def __init__(
        self,
        *,
        model_name: str,
        out: TextIO = sys.stdout,
        cwd: str | None = None,
        started_at: float | None = None,
    ):
        self.out = out
        self.model_name = model_name
        self.branch = _git_branch(cwd)
        self.started_at = started_at if started_at is not None else time.monotonic()
        self.thinking_on = False
        self._enabled = False
        self._rows = 0
        self._meter = None  # callable() -> int (active context tokens); set by the loop owner

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ---- lifecycle ----

    def enable(self) -> None:
        """Clear the screen, carve the scroll region, draw the initial bar."""
        if not USE_COLOR or not self.out.isatty():
            return  # non-tty (tests, single-shot): stay inert
        self._rows = shutil.get_terminal_size().lines
        if self._rows < 4:
            return  # tiny terminal: no room for a pinned bar — skip
        self._enabled = True
        print("\033[2J\033[H", end="", file=self.out)  # clear screen
        # scroll region = everything above the input line; the input line and
        # the status bar below it never scroll
        print(f"\033[1;{self._rows - 2}r", end="", file=self.out)
        self._move_to(self._rows - 2)  # bottom of the scroll region
        self.redraw()

    def disable(self) -> None:
        """Restore normal scrolling and park the cursor on a fresh line."""
        if not self._enabled:
            return
        print("\033[r", end="", file=self.out)
        print(file=self.out)
        self._enabled = False

    # ---- rendering ----

    def redraw(self) -> None:
        """Repaint the bar in place (after messages/events or state changes).

        Ends with the cursor back at the bottom of the scroll region — the
        next print (streamed delta, tool line, message) must land in the
        region, not on top of the bar (review R3)."""
        if not self._enabled:
            return
        self._move_to(self._rows)
        print("\033[2K" + self.render_text(), end="", file=self.out, flush=True)
        self._move_to(self._rows - 2)

    def move_to_input(self) -> None:
        """Park the cursor on the fixed input line (call before prompting)."""
        if not self._enabled:
            return
        self._move_to(self._rows - 1)

    def print_below(self, text: str) -> None:
        """Print one line into the scroll region (below its bottom edge),
        then repaint the bar — for transient notes like the Ctrl+O toggle."""
        if not self._enabled:
            return
        self._move_to(self._rows - 2)
        print(text, file=self.out)
        self.redraw()

    def after_submit(self) -> None:
        """The user just hit Enter: erase the submitted text from the input
        line and move into the scroll region so the reply renders above
        (previously the echoed input lingered on the fixed line)."""
        if not self._enabled:
            return
        self._move_to(self._rows - 1)
        print("\033[2K", end="", file=self.out)
        self._move_to(self._rows - 2)
        print(file=self.out)

    def render_text(self) -> str:
        """The bar's text (no cursor control) — unit-testable."""
        parts = [f"branch:{self.branch}", f"Model: {self.model_name}"]
        if self.thinking_on:
            parts.append(_c("thinking", CYAN))
        minutes = int((time.monotonic() - self.started_at) // 60)
        parts.append(f"session:{minutes}m")
        parts.append(self._ctx_meter())
        return " | ".join(parts)

    def _ctx_meter(self) -> str:
        tokens = self._meter() if self._meter is not None else 0
        window = DEFAULT_CONTEXT_WINDOW
        fraction = min(1.0, tokens / window) if window else 0.0
        filled = int(fraction * 10)
        meter = f"ctx:[{'#' * filled}{'-' * (10 - filled)}]{int(fraction * 100)}%"
        if fraction >= CRITICAL_FRACTION:
            return _c(meter + " CRITICAL", RED)
        if fraction >= WARN_FRACTION:
            return _c(meter, YELLOW)
        return meter

    def _move_to(self, row: int) -> None:
        print(f"\033[{row};1H", end="", file=self.out)
