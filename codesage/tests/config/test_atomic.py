"""Atomic write tests."""

import os

import pytest

from codesage.config import atomic_write


def test_write_and_read(tmp_path):
    target = tmp_path / "data.json"
    atomic_write(target, '{"a": 1}')
    assert target.read_text(encoding="utf-8") == '{"a": 1}'


def test_no_stray_tmp_files(tmp_path):
    target = tmp_path / "data.json"
    atomic_write(target, "hello")
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_overwrite(tmp_path):
    target = tmp_path / "data.json"
    atomic_write(target, "v1")
    atomic_write(target, "v2")
    assert target.read_text(encoding="utf-8") == "v2"


def test_read_json_lossy(tmp_path):
    from codesage.config import read_json_lossy

    (tmp_path / "ok.json").write_text('{"x": 1}', encoding="utf-8")
    assert read_json_lossy(tmp_path / "ok.json", {}) == {"x": 1}
    assert read_json_lossy(tmp_path / "missing.json", {"d": 1}) == {"d": 1}
    (tmp_path / "bad.json").write_text("[1,2]", encoding="utf-8")  # not a dict
    assert read_json_lossy(tmp_path / "bad.json", {"d": 1}) == {"d": 1}


def test_read_json_lossy_with_bom(tmp_path):
    # Notepad/PowerShell write a UTF-8 BOM by default; must not silently fail.
    from codesage.config import read_json_lossy

    (tmp_path / "bom.json").write_text('﻿{"x": 1}', encoding="utf-8")
    assert read_json_lossy(tmp_path / "bom.json", {}) == {"x": 1}


def test_write_through_symlink_keeps_link(tmp_path):
    target = tmp_path / "real.json"
    target.write_text("old", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlinks not permitted on this system")
    atomic_write(link, "new")
    assert link.is_symlink()
    assert os.path.realpath(link) == os.path.realpath(target)
    assert target.read_text(encoding="utf-8") == "new"


def test_preserves_existing_mode(tmp_path):
    target = tmp_path / "data.json"
    atomic_write(target, "v1")
    os.chmod(target, 0o600)
    mode_before = target.stat().st_mode & 0o7777
    atomic_write(target, "v2")
    assert target.read_text(encoding="utf-8") == "v2"
    assert (target.stat().st_mode & 0o7777) == mode_before


def test_retries_replace_after_permission_error(tmp_path, monkeypatch):
    """Windows: replace can fail with EPERM/EACCES (brief lock); retry once."""
    target = tmp_path / "data.json"
    target.write_text("old", encoding="utf-8")
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("win32: target locked")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    atomic_write(target, "new")
    assert calls["n"] == 2
    assert target.read_text(encoding="utf-8") == "new"
