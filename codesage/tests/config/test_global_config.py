"""Global config read/write tests."""

import json

from codesage.config import GlobalConfig, paths


def test_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    cfg = GlobalConfig(theme="dark")
    cfg.project("/abs/proj").allowed_tools.append("Bash")
    cfg.save()

    loaded = GlobalConfig.load()
    assert loaded.theme == "dark"
    assert loaded.projects["/abs/proj"].allowed_tools == ["Bash"]


def test_corrupt_degrades_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    (tmp_path / "config.json").write_text("garbage{", encoding="utf-8")
    cfg = GlobalConfig.load()
    assert cfg.theme is None
    assert cfg.projects == {}


def test_missing_degrades_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    assert GlobalConfig.load().projects == {}


def test_project_get_or_create(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    cfg = GlobalConfig()
    assert cfg.project("/x") is cfg.project("/x")
    assert set(cfg.projects) == {"/x"}


def test_model_pointers_default(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    cfg = GlobalConfig.load()
    assert cfg.model_pointers["main"] == "main"


def test_saved_file_is_valid_json(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    GlobalConfig(theme="x").save()
    assert json.loads((tmp_path / "config.json").read_text())["theme"] == "x"
