"""Slash command registry (CC-09): one place to add commands.

HELP_TEXT is generated from the registry, so the help output can never drift
from the actual command set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

#: handler(args, state) -> True = exit the REPL. *state* carries REPL flags:
#: {"show_thinking": bool, "loop": AgentLoop} — /mode writes loop.mode.
Handler = Callable[[list[str], dict], bool]


@dataclass(frozen=True)
class SlashCommand:
    name: str
    handler: Handler
    description: str
    aliases: list[str] = field(default_factory=list)


def _cmd_help(args: list[str], state: dict) -> bool:
    print(HELP_TEXT)
    return False


def _cmd_quit(args: list[str], state: dict) -> bool:
    print("bye")
    return True


def _cmd_mode(args: list[str], state: dict) -> bool:
    if len(args) == 1 and args[0] in ("plan", "default", "yolo"):
        state["loop"].mode = args[0]
        print(f"permission mode -> {args[0]}")
    else:
        print("usage: /mode plan|default|yolo")
    return False


def _cmd_show_thinking(args: list[str], state: dict) -> bool:
    state["show_thinking"] = not state["show_thinking"]
    print(f"show-thinking -> {state['show_thinking']}")
    return False


COMMANDS: list[SlashCommand] = [
    SlashCommand("mode", _cmd_mode, "switch permission mode (plan|default|yolo)"),
    SlashCommand("show-thinking", _cmd_show_thinking, "toggle thinking output"),
    SlashCommand("help", _cmd_help, "this help", aliases=["h"]),
    SlashCommand("quit", _cmd_quit, "exit", aliases=["q"]),
]


def find_command(name: str) -> SlashCommand | None:
    """Registry lookup by name or alias (leading '/' optional); None if unknown."""
    key = name.lstrip("/").lower()
    for cmd in COMMANDS:
        if key == cmd.name or key in cmd.aliases:
            return cmd
    return None


def _build_help_text() -> str:
    lines = ["Commands:"]
    lines += [f"  /{cmd.name}  {cmd.description}" for cmd in COMMANDS]
    lines.append("  (Ctrl+C once: interrupt the running turn; twice: exit)")
    return "\n".join(lines)


HELP_TEXT = _build_help_text()
