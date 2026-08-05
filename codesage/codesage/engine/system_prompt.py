"""System prompt assembly skeleton (phase 08 completes layering/AGENTS.md).

Phase 06 ships the minimal structure: base system + injected context blocks.
"""

from __future__ import annotations

from typing import Any


def build_system_prompt(base: str, context: dict[str, str] | None = None) -> str:
    """Compose the system prompt: base first, then labeled context sections."""
    if not context:
        return base
    sections = [base] if base else []
    for key, value in context.items():
        if value:
            sections.append(f"<{key}>\n{value}\n</{key}>")
    return "\n\n".join(sections)
