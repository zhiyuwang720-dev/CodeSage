"""Rule persistence tests: approval → settings.local.json → reload."""

import json
import os

import pytest

from codesage.permissions import PermissionEngine
from codesage.permissions.store import (
    SessionRuleStore,
    build_rule_string,
    build_session_rule,
    load_permission_rules,
    save_approval,
)


def test_save_approval_creates_file(tmp_path):
    path = tmp_path / "settings.local.json"
    save_approval(path, "Bash", "Bash")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == ["Bash"]


def test_save_approval_appends_without_duplicates(tmp_path):
    path = tmp_path / "settings.local.json"
    save_approval(path, "Bash", "Bash")
    save_approval(path, "Bash", "Bash")
    save_approval(path, "Grep", "Grep")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == ["Bash", "Grep"]


def test_save_approval_preserves_existing_settings(tmp_path):
    path = tmp_path / "settings.local.json"
    path.write_text(json.dumps({"hooks": {"enabled": True}}), encoding="utf-8")
    save_approval(path, "Bash", "Bash")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["hooks"] == {"enabled": True}
    assert data["permissions"]["allow"] == ["Bash"]


def test_load_permission_rules_from_settings():
    class FakeSettings:
        permissions = {"allow": ["Read"]}

    assert load_permission_rules(FakeSettings()) == {"allow": ["Read"]}
    assert load_permission_rules(None) == {}


# ---- A3: granular remember rules ----

def test_build_rule_string():
    assert build_rule_string("Bash", {"command": "git status"}) == "Bash(git status)"
    assert build_rule_string("Bash", {"command": "  git   status  "}) == "Bash(git status)"
    long_cmd = "echo " + "a" * 100
    assert build_rule_string("Bash", {"command": long_cmd}) == f"Bash({long_cmd[:80]})"
    assert build_rule_string("Edit", {"file_path": "/repo/proj/x.py"}) == "Edit(/repo/proj/**)"
    assert build_rule_string("Write", {"file_path": r"C:\repo\proj\x.py"}) == "Write(C:/repo/proj/**)"
    assert build_rule_string("Write", {"file_path": "x.py"}) == "Write"  # no usable parent
    assert build_rule_string("Read", {"file_path": "/x"}) == "Read"
    assert build_rule_string("Grep", {}) == "Grep"
    assert build_rule_string("LS", None) == "LS"


def test_save_approval_with_granular_rule(tmp_path):
    path = tmp_path / "settings.local.json"
    save_approval(path, "Bash", "Bash(git status)")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == ["Bash(git status)"]


def test_save_approval_rule_defaults_to_tool_name(tmp_path):
    path = tmp_path / "settings.local.json"
    save_approval(path, "Read")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == ["Read"]


def test_save_approval_through_symlink_keeps_link(tmp_path):
    """save_approval goes through atomic_write: the link survives, target is written."""
    real = tmp_path / "settings.local.json"
    real.write_text(json.dumps({}), encoding="utf-8")
    link = tmp_path / "link.local.json"
    try:
        os.symlink(real, link)
    except OSError:
        pytest.skip("symlinks not permitted on this system")
    save_approval(link, "Bash", "Bash")
    assert link.is_symlink()
    assert os.path.realpath(link) == os.path.realpath(real)
    assert json.loads(real.read_text(encoding="utf-8"))["permissions"]["allow"] == ["Bash"]


# ---- CC-07: session rules (in-memory, never persisted) ----

def test_build_session_rule_matches_build_rule_string():
    assert build_session_rule("Bash", {"command": "git status"}) == "Bash(git status)"
    assert build_session_rule("Edit", {"file_path": "/repo/x.py"}) == "Edit(/repo/**)"
    assert build_session_rule("Read", None) == "Read"


def test_session_rule_store_roundtrip():
    store = SessionRuleStore()
    assert store.rules() == {"allow": [], "deny": [], "ask": []}
    store.allow("Bash(git status)")
    store.allow("Bash(git status)")  # idempotent
    store.allow("Read")
    assert store.rules() == {"allow": ["Bash(git status)", "Read"], "deny": [], "ask": []}
    # the returned dict is a copy — mutating it must not corrupt the store
    store.rules()["allow"].append("mutate")
    assert store.rules()["allow"] == ["Bash(git status)", "Read"]


def test_session_rule_store_feeds_engine(tmp_path):
    """rules() output plugs straight into session_permissions (loop wiring)."""
    store = SessionRuleStore()
    store.allow("Bash")
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Bash", tool_input={"command": "ls"},
        session_permissions=store.rules(), cwd=tmp_path,
    )
    assert d.allowed and d.mode == "allow"
