"""Write-protection path tests."""

from pathlib import Path

from codesage.permissions.paths import is_sensitive_path, is_write_protected


def test_git_dir_protected():
    assert is_write_protected(Path("/repo/.git/config"))
    assert is_write_protected(Path("/repo/.git/objects/x"))


def test_ssh_protected():
    assert is_write_protected(Path("/home/u/.ssh/authorized_keys"))


def test_settings_files_protected():
    assert is_write_protected(Path("/repo/.codesage/settings.json"))
    assert is_write_protected(Path("/home/u/.codesage/config.json"))


def test_env_file_protected():
    assert is_write_protected(Path("/repo/.env"))


def test_normal_project_files_not_protected():
    assert not is_write_protected(Path("/repo/src/main.py"))
    assert not is_write_protected(Path("/repo/docs/hello.md"))


def test_protected_inside_any_depth():
    assert is_write_protected(Path("/repo/deep/path/.git/config"))


def test_sensitive_reads():
    assert is_sensitive_path(Path("/repo/.env"))
    assert not is_sensitive_path(Path("/repo/src/main.py"))
