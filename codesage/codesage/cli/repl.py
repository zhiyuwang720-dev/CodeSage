"""REPL (interactive) and single-shot (non-interactive) modes.

Single-shot mode is how the V1 acceptance test drives the harness: no UI,
ask decisions are denied (safe default).
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ..engine import AgentLoop
from .render import CYAN, RESET, _c, render_message


@dataclass
class RunSummary:
    """Machine-readable outcome of one single-turn run (--output-format json)."""

    session_id: str
    result: str
    num_turns: int
    usage: int
    total_cost_usd: float
    is_error: bool
    duration_seconds: float
    budget_exceeded: bool = False

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "result": self.result,
            "num_turns": self.num_turns,
            "usage": self.usage,
            "total_cost_usd": self.total_cost_usd,
            "is_error": self.is_error,
            "duration_seconds": self.duration_seconds,
        }

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
    render: bool = True,  # False for --output-format json (summary only)
) -> RunSummary:
    """One user input, rendered (also used by acceptance tests).

    Returns a RunSummary: is_error mirrors the old bool (exit code 1),
    budget_exceeded flags the max-budget stop message (exit code 1).
    """
    target = out or sys.stdout
    started = time.monotonic()
    has_error = False
    last_text = ""
    llm_calls = 0
    total_tokens = 0
    async for message in loop.run(user_input):
        has_error = has_error or (message.role == "assistant" and message.is_error)
        if render:
            render_message(message, out=target, show_thinking=show_thinking)
        if message.role != "assistant":
            continue
        if message.usage is not None:
            llm_calls += 1
            total_tokens += message.usage.total_tokens
        if isinstance(message.content, str):
            last_text = message.content
        else:
            text = "\n".join(b.text or "" for b in message.content if b.type == "text")
            if text:
                last_text = text
    client = getattr(loop, "client", None)
    budget_exceeded = "budget" in last_text.lower() or (
        getattr(loop, "max_budget_usd", None) is not None
        and getattr(client, "total_cost", None) is not None
        and client.total_cost[0] >= loop.max_budget_usd
    )
    return RunSummary(
        session_id=loop.session.path.stem if getattr(loop, "session", None) is not None else "",
        result=last_text,
        num_turns=max(llm_calls, 1),
        usage=total_tokens,
        total_cost_usd=float(getattr(client, "total_cost", [0.0])[0]),
        is_error=has_error,
        duration_seconds=time.monotonic() - started,
        budget_exceeded=budget_exceeded,
    )


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
