"""REPL (interactive) and single-shot (non-interactive) modes.

Single-shot mode is how the V1 acceptance test drives the harness: no UI,
ask decisions are denied (safe default).
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from ..engine import AgentLoop
from .render import CYAN, RESET, _c, render_message

HELP_TEXT = """Commands:
  /mode <plan|default|yolo>   switch permission mode
  /show-thinking              toggle thinking output
  /help                       this help
  /quit                       exit
  (Ctrl+C once: interrupt the running turn; twice: exit)"""


async def run_single_turn(
    loop: AgentLoop,
    user_input: str,
    *,
    show_thinking: bool = False,
    out=None,  # TextIO; default sys.stdout (injectable for tests)
) -> None:
    """One user input, rendered (also used by acceptance tests)."""
    import sys

    target = out or sys.stdout
    async for message in loop.run(user_input):
        render_message(message, out=target, show_thinking=show_thinking)


async def repl_loop(
    loop: AgentLoop,
    *,
    cwd: Path,
    show_thinking: bool = False,
) -> None:
    print(_c("CodeSage — V1 (type /help for commands, Ctrl+C to interrupt)", CYAN))
    _install_signal_handlers(loop)

    while True:
        try:
            line = await asyncio.to_thread(input, _c("\n❯ ", CYAN))
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return
        line = line.strip()
        if not line:
            continue
        if line.startswith("/"):
            if await _handle_slash_command(loop, line, lambda: show_thinking):
                return
            continue
        loop.abort.clear()
        await run_single_turn(loop, line, show_thinking=show_thinking)


async def _handle_slash_command(loop: AgentLoop, line: str, get_show_thinking) -> bool:
    parts = line.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    if cmd == "/quit":
        print("bye")
        return True
    if cmd == "/help":
        print(HELP_TEXT)
    elif cmd == "/mode":
        if arg in ("plan", "default", "yolo"):
            loop.mode = arg
            print(f"permission mode → {arg}")
        else:
            print("usage: /mode plan|default|yolo")
    elif cmd == "/show-thinking":
        print(f"show-thinking → {not get_show_thinking()} (restart to apply)")
    return False


def _install_signal_handlers(loop: AgentLoop) -> None:
    def on_sigint(signum, frame):
        if loop.abort.is_set():
            sys.exit(130)  # second Ctrl+C: hard exit
        loop.abort.set()

    if sys.platform != "win32":
        signal.signal(signal.SIGINT, on_sigint)
    else:
        # Windows: input() swallows SIGINT; the loop checks abort between
        # turns instead, and a second /quit exits.
        pass
