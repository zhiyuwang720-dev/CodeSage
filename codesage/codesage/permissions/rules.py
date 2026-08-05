"""Rule matching: tool-name rules and path rules (gitignore-ish semantics).

Tool-name rules: exact name, prefix ("Skill(foo:*)"), or glob ("mcp__*__*").
Path rules: absolute-path prefix with fnmatch on the tail ("/home/u/**"),
gitignore-style. Rules come from settings.permissions.{allow,deny,ask} and
the session's permission_rules (phase 12 persists session grants).
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

#: Rules whose value is a list of tool names or path strings.
RULE_KEYS = ("allow", "deny", "ask")


def extract_rules(permissions: dict[str, Any] | None) -> dict[str, list[Any]]:
    """Normalize the settings.permissions dict into {allow: [...], deny: [...], ask: [...]}."""
    out: dict[str, list[Any]] = {key: [] for key in RULE_KEYS}
    if not isinstance(permissions, dict):
        return out
    for key in RULE_KEYS:
        value = permissions.get(key)
        if isinstance(value, list):
            out[key] = value
        elif isinstance(value, str):
            out[key] = [value]
    return out


def tool_rule_matches(rule: str, tool_name: str) -> bool:
    """Match a tool-name rule: exact, glob, or "Name(pattern)" prefix form."""
    rule = rule.strip()
    if not rule:
        return False
    if rule == tool_name:
        return True
    if "*" in rule or "?" in rule:
        return fnmatch.fnmatch(tool_name, rule)
    # prefix form: "Skill(foo:*)" matches tools named Skill invoked with that skill;
    # simplified here as a glob on "tool(argument)" strings.
    return fnmatch.fnmatch(f"{tool_name}(*)", rule)


def path_rule_matches(rule: str, resolved_path: Path) -> bool:
    """Match a path rule against a resolved absolute path.

    Rules: "/abs/path" (prefix), "/abs/path/**" (recursive), "glob/**" handled
    via fnmatch against the string form. Relative rules are treated as
    prefix matches on the path string.
    """
    rule = rule.strip()
    if not rule:
        return False
    path_str = str(resolved_path).replace("\\", "/")
    rule_str = rule.replace("\\", "/")
    if rule_str.endswith("/**"):
        base = rule_str[:-3]
        return path_str.startswith(base) and path_str != base
    if rule_str.endswith("/*"):
        base = rule_str[:-2]
        return path_str.startswith(base + "/") and "/" not in path_str[len(base) + 1 :]
    if rule_str.startswith("/") and not rule_str.endswith("*"):
        return path_str == rule_str or path_str.startswith(rule_str.rstrip("/") + "/")
    return fnmatch.fnmatch(path_str, rule_str)


def match_first(rules: list[Any], tool_name: str, path: Path | None) -> str | None:
    """Return the rule string that matched (for audit/reason), or None.

    Gitignore-ish negation: a "!rule" entry (session rules commonly revoke
    settings rules this way) cancels any earlier match — the path/rule match
    fails and evaluation stops.
    # ponytail: simplified last-wins-by-negation, not full gitignore matching.
    """
    matched: str | None = None
    for rule in rules:
        if not isinstance(rule, str):
            continue
        if rule.startswith("!"):
            inner = rule[1:].strip()
            if inner and (
                (path is not None and path_rule_matches(inner, path))
                or tool_rule_matches(inner, tool_name)
            ):
                return None  # negation cancels all earlier matches
            continue
        if matched is None and (
            (path is not None and path_rule_matches(rule, path))
            or tool_rule_matches(rule, tool_name)
        ):
            matched = rule
    return matched
