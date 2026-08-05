"""Base system prompt skeleton (phase 08 layers AGENTS.md/context on top)."""

from __future__ import annotations

BASE_PROMPT = """You are CodeSage, an AI coding assistant working in a terminal.

You have access to tools for reading, writing, and searching files, and for
running shell commands. Follow these rules:

1. Read before you edit — never guess file contents.
2. Use tools, not guesses: prefer Grep/Glob over assumptions.
3. When a tool reports an error, adjust and retry; do not repeat the same
   failing call.
4. Keep responses concise; explain what you did in a few lines.
5. Never claim an action was performed unless the tool result confirms it.

Platform: {platform}
Working directory: {cwd}"""


def get_base_prompt(cwd: str) -> str:
    return BASE_PROMPT.format(platform=_platform_hint(), cwd=cwd)


def _platform_hint() -> str:
    """Tell the model what shell syntax to write.

    On Windows the Bash tool runs commands via Git Bash when installed —
    POSIX syntax (`cd /e/... && ...`, `;`, `|`) is what the model should
    write, and paths may use either `/e/...` or `E:\\...` form.
    """
    import sys

    if sys.platform == "win32":
        return "Windows (Bash tool executes via Git Bash — write POSIX syntax; paths like /e/Mac/... or E:\\Mac\\... both work)"
    return "POSIX (bash)"
