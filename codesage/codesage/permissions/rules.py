"""Rule matching: tool-name rules, path rules (gitignore-ish), Tool(content) rules.

Rule shapes:
- bare tool name ("Bash", "mcp__*__*") — tool-level match (exact/glob).
- bare path ("/home/u/**") — matches any file tool operating on that path.
- "ToolName(content)" — scoped to the tool class: file rules match by
  read/write set (Read/LS/Glob/Grep vs Write/Edit, Kode's read/edit
  separation), Bash rules are command patterns (see bash_rules_match), other
  content rules (Skill/WebFetch...) fall back to tool-level matching.

Rules come from settings.permissions.{allow,deny,ask} and the session's
permission_rules (phase 12 persists session grants).
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from .bash_rules import split_commands

#: Rules whose value is a list of tool names or path strings.
RULE_KEYS = ("allow", "deny", "ask")

#: File tools split by read/write set (Kode read/edit separation): a content
#: rule written for one tool applies to every tool in the same set.
READ_TOOLS = frozenset({"Read", "LS", "Glob", "Grep"})
WRITE_TOOLS = frozenset({"Write", "Edit"})
FILE_TOOLS = READ_TOOLS | WRITE_TOOLS


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


def parse_rule(rule: str) -> tuple[str | None, str | None]:
    """"ToolName(content)" → (tool_name, content); bare name → (name, None).
    Unparseable ("(" without ")", or empty tool name) → (None, None) so the
    rule never matches anything (Kode's parseToolRule returns null)."""
    rule = rule.strip()
    open_paren = rule.find("(")
    if open_paren == -1:
        return rule, None
    if not rule.endswith(")"):
        return None, None
    tool_name = rule[:open_paren].strip()
    if not tool_name:
        return None, None
    content = rule[open_paren + 1 : -1].strip()
    return tool_name, content or None


def rule_matches(rule: str, tool_name: str, path: Path | None) -> bool:
    """True if the rule applies to this tool use.

    Content rules only take effect when the rule's tool matches: file tools
    are grouped by read/write set (a "Read(...)" rule hits Read/LS/Glob/Grep,
    a "Write(...)"/"Edit(...)" rule hits Write/Edit), Bash content rules are
    command rules evaluated by the engine, everything else is tool-level.
    """
    parsed_name, content = parse_rule(rule)
    if parsed_name is None:
        return False
    if content is None:
        return (path is not None and path_rule_matches(rule, path)) or tool_rule_matches(rule, tool_name)
    if parsed_name in READ_TOOLS:
        return tool_name in READ_TOOLS and path is not None and path_rule_matches(content, path)
    if parsed_name in WRITE_TOOLS:
        return tool_name in WRITE_TOOLS and path is not None and path_rule_matches(content, path)
    if parsed_name == "Bash":
        # Bash(<cmd>) rules are command patterns (A2); the engine matches them
        # against the command text via bash_rules_match.
        return False
    return parsed_name == tool_name  # other content rules (Skill/WebFetch…): tool level


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
            if inner and rule_matches(inner, tool_name, path):
                return None  # negation cancels all earlier matches
            continue
        if matched is None and rule_matches(rule, tool_name, path):
            matched = rule
    return matched


# ---- Bash command rules (A2) ----

def _norm_ws(text: str) -> str:
    return " ".join(text.split())


def bash_rule_matches(rule_content: str, command: str) -> bool:
    """"Bash(<cmd>)" exact match, or "Bash(<prefix>*)" prefix wildcard —
    both with whitespace normalized."""
    content = _norm_ws(rule_content)
    cmd = _norm_ws(command)
    if content.endswith("*"):
        return cmd.startswith(content[:-1])
    return cmd == content


def _bash_content_rules(rules: list[Any]) -> list[tuple[str, str]]:
    """(content, rule_string) pairs for every Bash(<content>) rule."""
    out: list[tuple[str, str]] = []
    for rule in rules:
        if not isinstance(rule, str):
            continue
        name, content = parse_rule(rule)
        if name == "Bash" and content:
            out.append((content, rule))
    return out


def bash_rules_match(rules: list[Any], command: str, *, require_all: bool) -> str | None:
    """Match Bash(<content>) rules against a command at sub-command level.

    A whole-command exact match wins first (Kode's exactKey check before
    splitting). Otherwise the command is split on shell operators and each
    subcommand is matched independently:
    - require_all=False (deny/ask groups): any hit returns the rule — one
      denied subcommand denies the whole compound.
    - require_all=True (allow group): every subcommand must hit — a compound
      with an unruled subcommand is a mixed ask, not an allow.
    Returns None when no Bash content rule participates or nothing matches.
    """
    contents = _bash_content_rules(rules)
    if not contents:
        return None
    whole = _norm_ws(command)
    for content, rule in contents:
        if _norm_ws(content) == whole:
            return rule
    subs = [_norm_ws(s) for s in (split_commands(command) or [command])]
    hits: list[str] = []
    for sub in subs:
        for content, rule in contents:
            if bash_rule_matches(content, sub):
                hits.append(rule)
                break
    if not require_all:
        return hits[0] if hits else None
    return hits[0] if len(hits) == len(subs) else None
