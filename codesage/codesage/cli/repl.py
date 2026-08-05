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
) -> bool:
    """One user input, rendered (also used by acceptance tests).

    Returns True if an assistant error message was rendered (exit code 1).
    """
    import sys

    target = out or sys.stdout
    has_error = False
    async for message in loop.run(user_input):
        has_error = has_error or (message.role == "assistant" and message.is_error)
        render_message(message, out=target, show_thinking=show_thinking)
    return has_error


async def repl_loop(
    loop: AgentLoop,
    *,
    cwd: Path,
    show_thinking: bool = False,
) -> None:
    print(_c("CodeSage — V1 (type /help for commands, Ctrl+C to interrupt)", CYAN))
    _install_signal_handlers(loop)
    state = {"show_thinking": show_thinking}  # /show-thinking toggles this live

    while True:
        try:
            line = await asyncio.to_thread(input, _c(f"\n{_prompt_mark()} ", CYAN))
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return
        line = line.strip()
        if not line:
            continue
        if line.startswith("/"):
            if await _handle_slash_command(loop, line, state):
                return
            continue
        loop.abort.clear()
        await run_single_turn(loop, line, show_thinking=state["show_thinking"])


async def _handle_slash_command(loop: AgentLoop, line: str, state: dict) -> bool:
    """Handle one slash command; *state* carries mutable REPL flags. True = exit REPL."""
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
            print(f"permission mode -> {arg}")
        else:
            print("usage: /mode plan|default|yolo")
    elif cmd == "/show-thinking":
        state["show_thinking"] = not state["show_thinking"]
        print(f"show-thinking -> {state['show_thinking']}")
    return False


def _prompt_mark() -> str:
    """REPL prompt glyph, ASCII '>' when the output encoding can't render it."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "❯".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return ">"
    return "❯"


def _install_signal_handlers(loop: AgentLoop) -> None:
    """First SIGINT/SIGTERM aborts the running turn; a second exits 130."""

    def on_signal(signum, frame):
        if loop.abort.is_set():
            sys.exit(130)
        loop.abort.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass  # not in main thread / unsupported platform — best effort
