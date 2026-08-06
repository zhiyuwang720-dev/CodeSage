"""Grep tool: regex content search with line numbers.

Fast path: ripgrep (`rg`) as a subprocess — `rg --line-number --color never
--max-columns 500 -e <pattern> <path>`, plus -i/--glob/-A/-B/-C, 30s timeout.
Fallback: pure-Python walker (rg missing or failed). Both paths emit the same
`rel:lineno: content` lines, so output is identical either way.

# ponytail: the Python fallback re-reads every file (O(files x lines)); that
# is the performance ceiling — rg is the only fast path. Fine until trees grow
# past ~10k files, at which point rg becomes effectively mandatory.
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
import shutil
import sys
from pathlib import Path

from ...base import Tool, ToolResult, ToolUseContext
from ._common import MAX_RESULTS, SKIP_DIRS, resolve_root, walk_files

RG_TIMEOUT_S = 30

#: `rg` output line: <path>:<lineno>:<content>; lazy path prefix so a Windows
#: drive letter (`C:\...`) is consumed by the first group.
_RG_LINE_RE = re.compile(r"^(.*?):(\d+):(.*)$")


def _rg_args(
    pattern: str,
    root: Path,
    glob_filter: str,
    case_insensitive: bool,
    before: int,
    after: int,
) -> list[str]:
    args = ["--no-ignore", "--hidden", "--line-number", "--color", "never", "--max-columns", "500"]
    for d in SKIP_DIRS:  # mirror the Python walker's exclusions
        args += ["--glob", f"!{d}"]
    if case_insensitive:
        args.append("-i")
    if glob_filter:
        args += ["--glob", glob_filter]
    if before:
        args += ["-B", str(before)]
    if after:
        args += ["-A", str(after)]
    args += ["-e", pattern, str(root)]
    return args


async def _run_rg(args: list[str]) -> tuple[int | None, str]:
    """Run rg; returns (returncode, stdout) or (None, "") if rg is unavailable
    or fails to run (never raises)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "rg",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=sys.platform != "win32",
        )
    except OSError:
        return None, ""
    try:
        stdout_b, _stderr_b = await asyncio.wait_for(proc.communicate(), timeout=RG_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None, ""
    return proc.returncode or 0, stdout_b.decode("utf-8", errors="replace")


def _parse_rg_output(stdout: str, root: Path) -> list[tuple[str, int, str]]:
    """rg `path:lineno:content` lines -> (rel, lineno, content)."""
    parsed: list[tuple[str, int, str]] = []
    for line in stdout.splitlines():
        if line == "--":  # rg's group separator (only with -A/-B/-C)
            continue
        m = _RG_LINE_RE.match(line)
        if not m:
            continue
        try:
            rel = Path(m.group(1)).relative_to(root).as_posix()
        except ValueError:
            continue
        parsed.append((rel, int(m.group(2)), m.group(3).strip()))
    return parsed


class GrepTool(Tool):
    name = "Grep"
    description = (
        "Search file contents with a regex (case-sensitive by default; -i for case-insensitive). "
        "Supports a glob filename filter and context lines (-A/-B/-C)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression"},
            "path": {"type": "string", "description": "Root directory (default: cwd)"},
            "glob": {"type": "string", "description": "Filename filter, e.g. *.py"},
            "-i": {"type": "boolean", "description": "Case-insensitive"},
            "-n": {"type": "boolean", "description": "Show line numbers (default true)"},
            "-A": {"type": "integer", "description": "Lines of context after each match"},
            "-B": {"type": "integer", "description": "Lines of context before each match"},
            "-C": {"type": "integer", "description": "Lines of context before and after each match"},
        },
        "required": ["pattern"],
    }
    is_concurrency_safe = True

    def needs_permissions(self, input: dict) -> bool:
        return False  # read-only

    async def _run(self, input: dict, ctx: ToolUseContext) -> ToolResult:
        root = resolve_root(ctx, input.get("path"))
        glob_filter = str(input.get("glob") or "")
        show_numbers = input.get("-n", True)
        before = max(0, int(input.get("-B") or 0))
        after = max(0, int(input.get("-A") or 0))
        both = max(0, int(input.get("-C") or 0))
        if both:
            before = after = both
        pattern = str(input["pattern"])

        # Fast path: rg subprocess.
        if shutil.which("rg") is not None:
            code, stdout = await _run_rg(_rg_args(pattern, root, glob_filter, bool(input.get("-i")), before, after))
            if code is not None and code in (0, 1):  # 1 = no matches
                if code == 1:
                    return ToolResult("No matches")
                parsed = _parse_rg_output(stdout, root)
                return _render(parsed, show_numbers)

        # Fallback: rg missing/failed -> pure-Python walker (kept in sync
        # with rg output: same lines, same format, same MAX_RESULTS cap).
        try:
            regex = re.compile(pattern, re.IGNORECASE if input.get("-i") else 0)
        except re.error as exc:
            return ToolResult(f"Error: invalid regex: {exc}", is_error=True)
        results: list[str] = []
        for path in sorted(walk_files(root)):
            if glob_filter and not fnmatch.fnmatch(path.name, glob_filter):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            matches = [n for n, line in enumerate(lines, 1) if regex.search(line)]
            if not matches:
                continue
            rel = path.relative_to(root).as_posix()
            shown: set[int] = set()
            for lineno in matches:
                for n in range(max(1, lineno - before), min(len(lines), lineno + after) + 1):
                    if n in shown:
                        continue
                    shown.add(n)
                    prefix = f"{rel}:{n}: " if show_numbers else f"{rel}: "
                    results.append(prefix + lines[n - 1].strip())
                    if len(results) >= MAX_RESULTS:
                        return ToolResult("\n".join(results) + f"\n(truncated at {MAX_RESULTS} matches)")
        return ToolResult("\n".join(results) if results else "No matches")


def _render(parsed: list[tuple[str, int, str]], show_numbers: bool) -> ToolResult:
    lines = []
    for rel, lineno, content in parsed:
        prefix = f"{rel}:{lineno}: " if show_numbers else f"{rel}: "
        lines.append(prefix + content)
        if len(lines) >= MAX_RESULTS:
            return ToolResult("\n".join(lines) + f"\n(truncated at {MAX_RESULTS} matches)")
    return ToolResult("\n".join(lines) if lines else "No matches")
