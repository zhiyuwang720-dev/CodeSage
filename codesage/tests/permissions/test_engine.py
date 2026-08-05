"""Permission engine decision-chain matrix tests."""

import os

import pytest

from pathlib import Path

from codesage.permissions import PermissionEngine, PermissionMode
from codesage.tools import Tool
from codesage.tools.builtin.filesystem.read import ReadTool


def _engine(tool_name="Bash", **kw):
    return PermissionEngine().evaluate_tool_use(
        tool_name=tool_name, tool_input={"command": "ls"}, **kw
    )


class FakeReadOnlyTool(Tool):
    name = "ReadOnly"

    def needs_permissions(self, input) -> bool:
        return False


class FakeNormalTool(Tool):
    name = "NormalTool"


def test_system_whitelist():
    d = _engine(tool_name="TodoWrite")
    assert d.allowed and d.mode == "allow"


def test_deny_rule_wins_over_allow():
    d = _engine(permissions={"allow": ["Bash"], "deny": ["Bash"]})
    assert d.mode == "deny" and not d.allowed


def test_deny_beats_ask():
    d = _engine(permissions={"ask": ["Bash"], "deny": ["Bash"]})
    assert d.mode == "deny"


def test_ask_rule():
    d = _engine(permissions={"ask": ["Bash"]})
    assert d.mode == "ask" and not d.allowed


def test_allow_rule():
    d = _engine(permissions={"allow": ["Bash"]})
    assert d.allowed and d.mode == "allow"


def test_glob_rule_matches():
    d = _engine(tool_name="mcp__server__read", permissions={"allow": ["mcp__server__*"]})
    assert d.allowed


def test_deny_not_bypassed_by_yolo():
    """The critical safety property: yolo never overrides deny."""
    d = _engine(permissions={"deny": ["Bash"]}, mode=PermissionMode.YOLO)
    assert d.mode == "deny" and not d.allowed


def test_yolo_auto_allows_default_ask(tmp_path):
    # Write inside the working directory (not in REQUIRES_EXPLICIT_APPROVAL)
    # is auto-allowed under yolo
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Write", tool_input={"file_path": str(tmp_path / "x.txt"), "content": "x"},
        mode=PermissionMode.YOLO, cwd=tmp_path,
    )
    assert d.allowed and d.mode == "allow" and d.source == "yolo"


def test_yolo_write_outside_working_dir_asks(tmp_path):
    """The critical safety property: yolo never auto-allows out-of-tree writes."""
    target = tmp_path.parent / "outside.txt"
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Write", tool_input={"file_path": str(target), "content": "x"},
        mode=PermissionMode.YOLO, cwd=tmp_path,
    )
    assert d.mode == "ask" and d.requires_explicit_approval
    assert not d.allowed


def test_read_outside_working_dir_asks(tmp_path):
    target = tmp_path.parent / "elsewhere.txt"
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Read", tool_input={"file_path": str(target)}, cwd=tmp_path,
    )
    assert d.mode == "ask" and d.requires_explicit_approval


def test_working_dirs_param_expands_scope(tmp_path):
    inside = tmp_path / "sub" / "x.txt"
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Write", tool_input={"file_path": str(inside), "content": "x"},
        mode=PermissionMode.YOLO, cwd=tmp_path, working_dirs=[tmp_path / "sub"],
    )
    assert d.allowed


def test_explicit_allow_rule_wins_over_working_dir(tmp_path):
    """A user-whitelisted path outside the working dirs remains writable."""
    target = tmp_path.parent / "allowed.txt"
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Write", tool_input={"file_path": str(target), "content": "x"},
        permissions={"allow": [str(target)]}, cwd=tmp_path,
    )
    assert d.allowed and d.mode == "allow"


def test_bash_never_auto_allowed_even_in_yolo():
    """REQUIRES_EXPLICIT_APPROVAL tools ask even under yolo."""
    d = _engine(mode=PermissionMode.YOLO)
    assert d.mode == "ask" and d.requires_explicit_approval


def test_plan_mode_denies_writes():
    d = _engine(mode=PermissionMode.PLAN)
    assert d.mode == "deny" and d.source == "plan-mode"


def test_plan_mode_allows_read_only():
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Read", tool_input={"file_path": "/x"}, mode=PermissionMode.PLAN,
        tool=ReadTool(), cwd=Path("/"),
    )
    assert d.allowed


def test_sensitive_read_requires_explicit_approval():
    """Reading .env must ask even though Read self-declares no permissions."""
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Read", tool_input={"file_path": "/repo/.env"}, tool=ReadTool(),
        cwd=Path("/"),
    )
    assert d.mode == "ask" and d.requires_explicit_approval


def test_self_declared_no_permissions():
    d = PermissionEngine().evaluate_tool_use(
        tool_name="ReadOnly", tool=FakeReadOnlyTool(), mode=PermissionMode.DEFAULT
    )
    assert d.allowed and d.source == "self-declared"


def test_default_is_ask_for_unknown():
    d = _engine()
    assert d.mode == "ask" and not d.allowed


def test_path_deny_rule_for_file_tool(tmp_path):
    target = tmp_path / "secret.txt"
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Write",
        tool_input={"file_path": str(target), "content": "x"},
        permissions={"deny": [str(target)]},
        cwd=tmp_path,
    )
    assert d.mode == "deny"


# ---- CC-06: rules match the lexical path too (pre-resolution) ----

def test_deny_rule_matches_lexical_dotdot_path(tmp_path):
    """A deny rule on the unexpanded spelling (.. segments intact) hits the
    lexical candidate even though the real path collapses elsewhere."""
    lexical = str(tmp_path / "sub" / ".." / "secret.txt")
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Read",
        tool_input={"file_path": lexical},
        permissions={"deny": [lexical]},
        cwd=tmp_path,
    )
    assert d.mode == "deny"


def test_deny_rule_matches_symlink_lexical_path(tmp_path):
    """/tmp/link → ~/.ssh/config: a deny on the link spelling must still hit —
    the resolved realpath would never match the lexical rule."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not permitted on this system")
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Read",
        tool_input={"file_path": str(link / "config")},
        permissions={"deny": [str(link / "**")]},
        cwd=tmp_path,
    )
    assert d.mode == "deny"


def test_write_protected_path_requires_explicit_approval():
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Write",
        tool_input={"file_path": "/repo/.git/config", "content": "x"},
        permissions={"allow": ["/repo/.git/config"]},  # even an explicit allow
        cwd=__import__("pathlib").Path("/"),
    )
    assert d.mode == "ask" and d.requires_explicit_approval


def test_write_protection_wins_over_yolo():
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Edit",
        tool_input={"file_path": "/repo/.env", "old_string": "a", "new_string": "b"},
        mode=PermissionMode.YOLO,
        cwd=__import__("pathlib").Path("/"),
    )
    assert d.mode == "ask" and d.requires_explicit_approval


def test_session_rules_merge_with_settings():
    d = _engine(permissions={"allow": ["Bash"]}, session_permissions={"deny": ["Bash"]})
    assert d.mode == "deny"


def test_session_negation_revokes_settings_allow():
    """Later session !rule cancels an earlier settings allow (Kode semantics)."""
    d = _engine(permissions={"allow": ["Bash"]}, session_permissions={"allow": ["!Bash"]})
    assert d.mode == "ask" and not d.allowed


def test_bash_rules_deny_wins_over_explicit_allow(tmp_path):
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Bash", tool_input={"command": "rm -rf /"},
        permissions={"allow": ["Bash"]}, cwd=tmp_path,
    )
    assert d.mode == "deny" and d.source == "bash-rules"


def test_bash_rules_ask_requires_explicit_approval(tmp_path):
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Bash", tool_input={"command": "echo hi > /etc/passwd"}, cwd=tmp_path,
    )
    assert d.mode == "ask" and d.requires_explicit_approval


def test_bash_rules_allow_continues_normal_chain(tmp_path):
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Bash", tool_input={"command": "ls"}, permissions={"allow": ["Bash"]}, cwd=tmp_path,
    )
    assert d.allowed and d.mode == "allow" and d.source == "Bash"


def test_bash_rules_ask_not_bypassed_by_yolo(tmp_path):
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Bash", tool_input={"command": "rm /etc/passwd"},
        mode=PermissionMode.YOLO, cwd=tmp_path,
    )
    assert d.mode == "ask" and d.requires_explicit_approval


def test_unknown_mode_normalizes_to_default():
    d = _engine(mode="weird-mode")
    assert d.mode == "ask"


# ---- A1: Tool(content) rules through the engine ----

def test_content_allow_read_rule_scoped(tmp_path):
    """allow:["Read(<tmp>/**)"] — only Reads inside the dir, never Writes."""
    rule = f"Read({tmp_path}/**)"
    inside = str(tmp_path / "a.txt")
    outside = str(tmp_path.parent / "b.txt")
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Read", tool_input={"file_path": inside},
        permissions={"allow": [rule]}, cwd=tmp_path,
    )
    assert d.allowed and d.source == rule
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Read", tool_input={"file_path": outside},
        permissions={"allow": [rule]}, cwd=tmp_path,
    )
    assert not d.allowed
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Write", tool_input={"file_path": inside, "content": "x"},
        permissions={"allow": [rule]}, cwd=tmp_path,
    )
    assert not d.allowed


def test_content_deny_edit_rule_does_not_block_read(tmp_path):
    """deny:["Edit(<tmp>/**)"] must not block Reads."""
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Read", tool_input={"file_path": str(tmp_path / "x.py")},
        permissions={"deny": [f"Edit({tmp_path}/**)"]}, tool=ReadTool(), cwd=tmp_path,
    )
    assert d.allowed  # Read self-declares no permissions


def test_write_protection_beats_explicit_allow(tmp_path):
    """A write-protected path stays ask even when an allow rule matches."""
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Write",
        tool_input={"file_path": str(tmp_path / ".git" / "config"), "content": "x"},
        permissions={"allow": [f"{tmp_path}/**"]}, cwd=tmp_path,
    )
    assert d.mode == "ask" and d.requires_explicit_approval


# ---- A2: Bash command rules through the engine ----

def test_bash_content_deny_rule(tmp_path):
    rules = {"deny": ["Bash(rm *)"]}
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Bash", tool_input={"command": "rm -rf x"}, permissions=rules, cwd=tmp_path,
    )
    assert d.mode == "deny" and d.source == "Bash(rm *)"
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Bash", tool_input={"command": "ls"}, permissions=rules, cwd=tmp_path,
    )
    assert d.mode != "deny"  # the rule does not block ls


def test_bash_content_allow_exact_rule(tmp_path):
    rules = {"allow": ["Bash(git status)"]}
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Bash", tool_input={"command": "git  status"}, permissions=rules, cwd=tmp_path,
    )
    assert d.allowed and d.source == "Bash(git status)"
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Bash", tool_input={"command": "ls"}, permissions=rules, cwd=tmp_path,
    )
    assert d.mode == "ask"


def test_bash_content_rule_subcommand_level(tmp_path):
    """One denied subcommand denies the compound; mixed → ask."""
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Bash", tool_input={"command": "git status && rm -rf x"},
        permissions={"deny": ["Bash(rm *)"]}, cwd=tmp_path,
    )
    assert d.mode == "deny"
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Bash", tool_input={"command": "git status && git diff"},
        permissions={"allow": ["Bash(git status)"]}, cwd=tmp_path,
    )
    assert d.mode == "ask"  # mixed: git diff has no rule
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Bash", tool_input={"command": "git status && git log"},
        permissions={"allow": ["Bash(git status)", "Bash(git log)"]}, cwd=tmp_path,
    )
    assert d.allowed and d.mode == "allow"
