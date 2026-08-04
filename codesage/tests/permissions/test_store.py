"""Rule persistence tests: approval → settings.local.json → reload."""

import json

from codesage.permissions.store import load_permission_rules, save_approval


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
