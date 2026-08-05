"""CLI (phase 07): interactive REPL + single-shot mode. V1 acceptance entry.

Exit codes: 0 success; 1 LLM error turn / empty piped stdin / resume target
missing / safe-mode root refusal; 2 argparse usage errors (argparse handles).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .. import __version__
from ..config import paths
from ..core import Session, find_session, most_recent_session
from .assemble import apply_tool_filter, build_loop, session_root
from .permission_prompt import request_permission
from .render import render_message
from .repl import repl_loop, run_single_turn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codesage", description="CodeSage harness (V1)")
    parser.add_argument("prompt", nargs="?", help="single-shot prompt (omit for REPL)")
    parser.add_argument("--cwd", type=Path, default=paths.cwd(), help="working directory")
    parser.add_argument("--mode", default="default", choices=["plan", "default", "yolo"])
    parser.add_argument("--model", default="main")
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument("--show-thinking", action="store_true")
    parser.add_argument("--safe", action="store_true", help="lock permission mode to default (never yolo)")
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument("--resume", action="store_true", help="resume the most recent session")
    resume_group.add_argument("--session-id", metavar="ID", help="resume a specific session by id")
    parser.add_argument("--allowedTools", metavar="N1,N2", help="comma-separated tool allowlist")
    parser.add_argument("--disallowedTools", metavar="N1,N2", help="comma-separated tool denylist")
    parser.add_argument("--version", action="version", version=f"codesage {__version__}")
    args = parser.parse_args(argv)

    # piped stdin with no prompt argument = single-turn mode
    prompt = args.prompt
    if prompt is None and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
        if not prompt:
            print("no prompt given — stdin is empty (usage below)", file=sys.stderr)
            print(parser.format_usage(), file=sys.stderr)
            return 1

    if args.safe:
        if args.mode != "default":
            print(f"--safe: forcing --mode {args.mode} -> default", file=sys.stderr)
        print("safe mode: permission mode locked to default", file=sys.stderr)
        # POSIX root check only; Windows admin detection is skipped (getuid absent).
        if not sys.platform.startswith("win") and getattr(os, "getuid", lambda: -1)() == 0:
            print(
                "refusing to run in safe mode as root — use a non-root user, or --mode yolo knowingly",
                file=sys.stderr,
            )
            return 1
        mode = "default"
    else:
        mode = args.mode

    # resume: show history, then start a fresh session in the same project dir
    # (history is not fed to the model — degraded resume, all in the CLI layer).
    cwd = args.cwd.resolve()
    root = session_root()
    resumed = None
    if args.session_id:
        resumed = find_session(root, args.session_id)
        if resumed is None:
            print(f"session not found: {args.session_id}", file=sys.stderr)
            return 1
    elif args.resume:
        resumed = most_recent_session(root)
        if resumed is None:
            print("no sessions to resume", file=sys.stderr)
            return 1
    if resumed is not None:
        _print_history_summary(resumed, root)

    project_key = None
    if resumed is not None and resumed.parent != root:
        project_key = resumed.parent.name

    loop = build_loop(
        cwd=cwd,
        mode=mode,
        model=args.model,
        max_turns=args.max_turns,
        request_permission=None if prompt else _interactive_permission(),
        project_key=project_key,
    )
    apply_tool_filter(loop, args.allowedTools, args.disallowedTools)

    if prompt:
        # single-shot: no UI for permission asks — denials go back to the model
        has_error = asyncio.run(run_single_turn(loop, prompt, show_thinking=args.show_thinking))
        return 1 if has_error else 0
    asyncio.run(repl_loop(loop, cwd=cwd, show_thinking=args.show_thinking))
    return 0


def _print_history_summary(path: Path, root: Path, limit: int = 10) -> None:
    """Resume: print the last *limit* messages of a session, then a new turn starts."""
    project_key = path.parent.name if path.parent != root else None
    messages = Session(path.stem, root, project_key).load()
    print(f"[resuming {path.stem}: {len(messages)} message(s), showing last {min(limit, len(messages))}]")
    for message in messages[-limit:]:
        render_message(message)


def _interactive_permission():
    local_settings = paths.local_settings_path()
    return lambda decision, tool, tool_input: request_permission(
        decision, tool, tool_input, local_settings_path=local_settings
    )


if __name__ == "__main__":
    sys.exit(main())
