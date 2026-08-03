"""Settings three-tier merge tests."""

import json

import pytest

from codesage.config import SettingsStore, paths


@pytest.fixture
def store(tmp_path, monkeypatch):
    """SettingsStore with all three tiers pointing into tmp_path."""
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / ".codesage")
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".codesage").mkdir()
    store = SettingsStore(project_dir=project)
    return store, project


def write_tier(tmp_path, project, tier, data):
    path = {
        "user": tmp_path / ".codesage" / "settings.json",
        "project": project / ".codesage" / "settings.json",
        "local": project / ".codesage" / "settings.local.json",
    }[tier]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_single_tier(store):
    s, project = store
    write_tier(s._project_dir.parent, project, "user", {"permissions": {"allow": ["Read"]}})
    settings = s.load()
    assert settings.permissions["allow"] == ["Read"]


def test_tier_override_precedence(store):
    s, project = store
    tmp = s._project_dir.parent
    write_tier(tmp, project, "user", {"permissions": {"allow": ["Read", "Grep"]}})
    write_tier(tmp, project, "local", {"permissions": {"allow": ["Bash"]}})
    settings = s.load()
    # local (highest) appends, user stays first
    assert settings.permissions["allow"] == ["Read", "Grep", "Bash"]


def test_deep_merge_scalar_override(store):
    s, project = store
    tmp = s._project_dir.parent
    write_tier(tmp, project, "user", {"permissions": {"mode": "plan"}})
    write_tier(tmp, project, "local", {"permissions": {"mode": "yolo"}})
    assert s.load().permissions["mode"] == "yolo"


def test_missing_files_are_empty(store):
    s, project = store
    assert s.load() == s.load()  # no error, no content


def test_corrupt_file_degrades(store):
    s, project = store
    write_tier(s._project_dir.parent, project, "user", "{not json")
    settings = s.load()
    assert settings.permissions == {}


def test_unknown_keys_preserved(store):
    s, project = store
    write_tier(s._project_dir.parent, project, "user", {"future_key": {"x": 1}})
    assert s.load().future_key == {"x": 1}


def test_mtime_cache_invalidates(store):
    s, project = store
    tmp = s._project_dir.parent
    write_tier(tmp, project, "user", {"permissions": {"allow": ["Read"]}})
    s.load()
    write_tier(tmp, project, "user", {"permissions": {"allow": ["Bash"]}})
    assert s.load().permissions["allow"] == ["Bash"]


def test_load_settings_convenience(store):
    s, project = store
    write_tier(s._project_dir.parent, project, "user", {"hooks": {"a": 1}})
    from codesage.config import load_settings

    assert load_settings(project_dir=project).hooks == {"a": 1}
