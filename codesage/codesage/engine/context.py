"""Session context bundle (phase 08, specs/08 §3.3).

Built ONCE per session (memoize = the CLI calls this once and holds the
bundle; nothing here runs per turn). Sections are fixed-order tuples the
renderer turns into system-reminder payloads (S4): date → git snapshot →
AGENTS.md (far → near, so the near file lands last — recency bias).

Synchronous on purpose: the composition root (build_loop) is sync; the git
queries still run in parallel via a thread pool.
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

#: Total budget for all AGENTS.md content (todo.md acceptance: 32KB truncation).
MAX_AGENTS_CHARS = 32 * 1024
#: git status --short output cap (Claude Code MAX_STATUS_CHARS).
MAX_STATUS_CHARS = 2_000
#: git commands never take optional locks (avoid fighting other git ops).
_GIT_FLAGS = ["--no-optional-locks"]

DISCLAIMER = (
    "This is the git status at the start of the conversation. "
    "Note that this status is a snapshot in time, and will not update during the conversation."
)


@dataclass
class ContextBundle:
    """Session context: (title, text) pairs in render order."""

    sections: list[tuple[str, str]] = field(default_factory=list)

    def get(self, title: str) -> str | None:
        return next((text for t, text in self.sections if t == title), None)


def _collect_agents_files(cwd: Path) -> list[tuple[Path, str]]:
    """AGENTS.md files from cwd upward, far → near (near file is last)."""
    files: list[tuple[Path, str]] = []
    p = cwd.resolve()
    while True:
        candidate = p / "AGENTS.md"
        if candidate.is_file():
            try:
                content = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            if content.strip():
                files.append((candidate, content))
        if p == p.parent:
            break
        p = p.parent
    files.reverse()  # cwd was collected first: flip to far → near
    return files


def _apply_budget(files: list[tuple[Path, str]]) -> list[str]:
    """Near files stay complete; the first far file that overflows is
    truncated to the remainder; anything farther is dropped."""
    remaining = MAX_AGENTS_CHARS
    kept: list[str] = []
    for _path, content in reversed(files):  # near → far
        if remaining <= 0:
            break
        if len(content) <= remaining:
            kept.append(content)
            remaining -= len(content)
        else:
            kept.append(content[:remaining])
            remaining = 0
    kept.reverse()  # far → near again for render order
    return kept


def _read_override(override_file: Path) -> list[str]:
    """Override replaces automatic AGENTS.md discovery entirely."""
    try:
        content = override_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not content.strip():
        return []
    return [content[:MAX_AGENTS_CHARS]]


def _git_run(cwd: Path, args: list[str]) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):  # git missing / hung
        return ""
    return out.stdout.decode("utf-8", errors="replace").strip()


def _git_snapshot(cwd: Path) -> str | None:
    """branch + recent commits + short status (capped), or None outside a repo."""
    if _git_run(cwd, ["rev-parse", "--is-inside-work-tree"]) != "true":
        return None
    with ThreadPoolExecutor(max_workers=3) as pool:
        branch = pool.submit(_git_run, cwd, [*_GIT_FLAGS, "branch", "--show-current"]).result()
        log = pool.submit(_git_run, cwd, [*_GIT_FLAGS, "log", "--oneline", "-n", "5"]).result()
        status = pool.submit(_git_run, cwd, [*_GIT_FLAGS, "status", "--short"]).result()
    if len(status) > MAX_STATUS_CHARS:
        status = status[:MAX_STATUS_CHARS] + f"\n... (truncated beyond {MAX_STATUS_CHARS} chars)"
    return "\n\n".join(
        [
            DISCLAIMER,
            f"Current branch: {branch or '(detached)'}",
            f"Recent commits:\n{log or '(none)'}",
            f"Status:\n{status or '(clean)'}",
        ]
    )


def build_context_bundle(cwd: Path, *, override_file: Path | None = None) -> ContextBundle:
    """Assemble the session context once (memoize semantics live at the caller)."""
    sections: list[tuple[str, str]] = [
        ("currentDate", f"Today's date is {date.today().isoformat()}.")
    ]
    snapshot = _git_snapshot(cwd)
    if snapshot is not None:
        sections.append(("gitStatus", snapshot))
    agents = _read_override(override_file) if override_file is not None else _apply_budget(_collect_agents_files(cwd))
    sections.extend(("agentsMd", content) for content in agents)
    return ContextBundle(sections)
