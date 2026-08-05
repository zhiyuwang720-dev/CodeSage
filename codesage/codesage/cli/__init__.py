"""CLI (phase 07): interactive REPL + single-shot mode. V1 acceptance entry.

Exit codes: 0 success; 1 LLM error turn / USD budget exceeded / max turns
exceeded / empty piped stdin / --print with no input / resume target
missing / safe-mode root refusal / --system-prompt-file unreadable; 2
argparse usage errors.

单轮/headless 模式(--print/--headless,或 stdout 非 tty 且输入存在)无权限
UI:ask 决策一律拒绝并回传模型(可用 --mode yolo + --allowedTools 精确放行)。

--max-budget-usd 触限时打印 "Error: Exceeded USD budget" 到 stderr 并 exit 1
(比 Kode print 模式的 exit 0 更符合脚本语义:非零表示预算未完成)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from .. import __version__
from ..config import paths
from ..core import Session, find_session, most_recent_session
from .assemble import apply_tool_filter, build_loop, session_root
from .permission_prompt import request_permission
from .render import render_message
from .repl import _install_single_shot_sigint, repl_loop, run_single_turn


def _configure_logging(verbose: bool, debug: bool) -> None:
    """--debug → DEBUG, --verbose → INFO, both on stderr. The --debug filter
    value is accepted but not used (ponytail: root-level DEBUG, per-module
    filters when a category filter ever needs to exist)."""
    level = logging.DEBUG if debug else (logging.INFO if verbose else None)
    if level is None:
        return
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codesage", description="CodeSage harness (V1)")
    parser.add_argument("prompt", nargs="?", help="single-shot prompt (omit for REPL)")
    parser.add_argument("--cwd", type=Path, default=paths.cwd(), help="working directory")
    parser.add_argument("--mode", default="default", choices=["plan", "default", "yolo"])
    parser.add_argument("--model", default="main")
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument("--max-budget-usd", type=float, default=None,
                        help="stop after this USD spend (single-turn; exceeded → exit 1)")
    parser.add_argument("--show-thinking", action="store_true")
    parser.add_argument("--safe", action="store_true", help="lock permission mode to default (never yolo)")
    parser.add_argument("-p", "--print", action="store_true",
                        help="single-turn non-interactive mode (no permission UI; ask decisions are denied)")
    parser.add_argument("--headless", action="store_true", help="synonym for --print")
    parser.add_argument("--output-format", choices=["text", "json"], default="text",
                        help="json: emit one result object to stdout after the turn (forces single-turn)")
    parser.add_argument("--debug", nargs="?", const="", default=None, metavar="FILTER",
                        help="debug logging to stderr (optional category filter)")
    parser.add_argument("--verbose", action="store_true", help="INFO logging to stderr")
    prompt_group = parser.add_mutually_exclusive_group()
    prompt_group.add_argument("--system-prompt", metavar="TEXT", help="override the system prompt")
    prompt_group.add_argument("--system-prompt-file", metavar="PATH", help="read the system prompt from a file")
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument("--resume", action="store_true", help="resume the most recent session")
    resume_group.add_argument("--session-id", metavar="ID", help="resume a specific session by id")
    parser.add_argument("--allowedTools", metavar="N1,N2", help="comma-separated tool allowlist")
    parser.add_argument("--disallowedTools", metavar="N1,N2", help="comma-separated tool denylist")
    parser.add_argument("--version", action="version", version=f"codesage {__version__}")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose, args.debug is not None)

    # single-turn detection (Kode headlessMode semantics): explicit
    # --print/--headless, or stdout not a tty with a prompt or piped stdin.
    print_mode = args.print or args.headless or (args.output_format == "json")
    prompt = args.prompt
    stdin_read = False
    if print_mode or not sys.stdout.isatty() or not sys.stdin.isatty():
        if prompt is None and not sys.stdin.isatty():
            prompt = sys.stdin.read().strip() or None
            stdin_read = True
        if prompt is None and (print_mode or stdin_read):
            why = (
                "--print: no prompt given — pass a prompt argument or pipe stdin"
                if print_mode
                else "no prompt given — stdin is empty"
            )
            print(f"{why} (usage below)", file=sys.stderr)
            print(parser.format_usage(), file=sys.stderr)
            return 1
        if prompt is not None and (print_mode or not sys.stdout.isatty()):
            print_mode = True

    system_prompt = args.system_prompt
    if args.system_prompt_file:
        try:
            system_prompt = Path(args.system_prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"cannot read --system-prompt-file: {exc}", file=sys.stderr)
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
        max_budget_usd=args.max_budget_usd,
        request_permission=None if prompt else _interactive_permission(),
        project_key=project_key,
        system_prompt=system_prompt,
    )
    apply_tool_filter(loop, args.allowedTools, args.disallowedTools)

    if prompt:
        # single-shot: no UI for permission asks — denials go back to the model
        _install_single_shot_sigint(loop)  # CC-11: Ctrl+C aborts + exits 130
        summary = asyncio.run(
            run_single_turn(
                loop,
                prompt,
                show_thinking=args.show_thinking,
                render=args.output_format != "json",
            )
        )
        if args.output_format == "json":
            print(json.dumps(summary.to_dict(), ensure_ascii=False))
        if summary.budget_exceeded:
            print("Error: Exceeded USD budget", file=sys.stderr)
            return 1
        if summary.max_turns_exceeded:
            print("Error: Exceeded max turns", file=sys.stderr)
            return 1
        return 1 if summary.is_error else 0
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
