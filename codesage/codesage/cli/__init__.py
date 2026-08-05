"""CLI (phase 07): interactive REPL + single-shot mode. V1 acceptance entry."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ..config import paths
from .assemble import build_loop
from .permission_prompt import request_permission
from .repl import repl_loop, run_single_turn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codesage", description="CodeSage harness (V1)")
    parser.add_argument("prompt", nargs="?", help="single-shot prompt (omit for REPL)")
    parser.add_argument("--cwd", type=Path, default=paths.cwd(), help="working directory")
    parser.add_argument("--mode", default="default", choices=["plan", "default", "yolo"])
    parser.add_argument("--model", default="main")
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument("--show-thinking", action="store_true")
    args = parser.parse_args(argv)

    cwd = args.cwd.resolve()
    show_thinking = args.show_thinking

    loop = build_loop(
        cwd=cwd,
        mode=args.mode,
        model=args.model,
        max_turns=args.max_turns,
        request_permission=None if args.prompt else _interactive_permission(),
    )

    if args.prompt:
        # single-shot: no UI for permission asks — denials go back to the model
        asyncio.run(run_single_turn(loop, args.prompt, show_thinking=show_thinking))
    else:
        asyncio.run(repl_loop(loop, cwd=cwd, show_thinking=show_thinking))
    return 0


def _interactive_permission():
    local_settings = paths.local_settings_path()
    return lambda decision, tool, tool_input: request_permission(
        decision, tool, tool_input, local_settings_path=local_settings
    )


if __name__ == "__main__":
    sys.exit(main())
