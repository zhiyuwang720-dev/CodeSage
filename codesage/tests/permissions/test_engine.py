"""Permission engine decision-chain matrix tests."""

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


def test_yolo_auto_allows_default_ask():
    # Write (not in REQUIRES_EXPLICIT_APPROVAL) is auto-allowed under yolo
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Write", tool_input={"file_path": "/tmp/x.txt", "content": "x"},
        mode=PermissionMode.YOLO, cwd=Path("/"),
    )
    assert d.allowed and d.mode == "allow" and d.source == "yolo"


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
        tool=ReadTool(),
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


def test_unknown_mode_normalizes_to_default():
    d = _engine(mode="weird-mode")
    assert d.mode == "ask"
