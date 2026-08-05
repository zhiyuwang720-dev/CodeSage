"""Global config read/write tests."""

import errno
import json

import pytest

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


@pytest.mark.parametrize("err", [errno.EACCES, errno.EPERM, errno.EROFS])
def test_save_degrades_on_denied_write(tmp_path, monkeypatch, err):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    from codesage.config import global_config

    def denied(path, content):
        raise PermissionError(err, "denied", str(path))

    monkeypatch.setattr(global_config, "atomic_write", denied)
    GlobalConfig(theme="dark").save()  # must not raise


def test_save_still_raises_other_os_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    from codesage.config import global_config

    def broken(path, content):
        raise OSError(errno.EISDIR, "is a directory", str(path))

    monkeypatch.setattr(global_config, "atomic_write", broken)
    with pytest.raises(OSError):
        GlobalConfig().save()
