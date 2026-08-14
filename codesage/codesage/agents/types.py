"""Agent definition model (phase 13 S1): frozen dataclass with whitelist fields.

Fields mirror the CC BaseAgentDefinition subset that phase 13 consumes
(spec §3.2). Unknown frontmatter keys are ignored at load time — this model
only ever holds whitelisted fields.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class AgentDefinition:
    """A subagent type: frontmatter + body used as the subagent system prompt.

    *tools* None means "full parent pool (post-filter)"; a set means a
    whitelist. *disallowed_tools* is applied on top either way.
    """

    name: str  # frontmatter name; missing name → file silently skipped
    description: str  # when-to-use, injected into the Agent tool description
    body: str  # text after the frontmatter fence = subagent system prompt
    tools: frozenset[str] | None = None  # None = full parent pool; else whitelist
    disallowed_tools: frozenset[str] = frozenset()
    model: str | None = None  # 'inherit', pointer, or literal; None ≡ 'inherit'
    max_turns: int | None = 50  # None = inherit parent value
    permission_mode: str | None = None  # None = inherit parent mode
    fork_context: bool = False  # True → loader forces model='inherit'
    hooks: dict | None = None  # parsed & stored only; execution in phase 19
    background: bool = False  # stored only; explicit run_in_background wins
    color: str | None = None  # stored only
    source: str = "project"  # 'builtin' | 'user' | 'project'
