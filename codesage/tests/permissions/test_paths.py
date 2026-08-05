"""Write-protection path tests."""

import os

import pytest

from pathlib import Path

from codesage.permissions.paths import is_sensitive_path, is_write_protected, resolve_candidates


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


def test_shell_and_vcs_config_files_protected():
    for name in [
        ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile",
        ".zshrc", ".zprofile", ".profile", ".mcp.json",
    ]:
        assert is_write_protected(Path("/home/u") / name), name


def test_ide_dirs_protected():
    assert is_write_protected(Path("/repo/.vscode/settings.json"))
    assert is_write_protected(Path("/repo/.idea/workspace.xml"))


def test_windows_reserved_names_protected():
    for name in ["CON", "con.txt", "PRN", "aux.log", "NUL", "COM1.txt", "COM9", "LPT3.dat", "LPT9"]:
        assert is_write_protected(Path("C:/repo") / name), name
    assert not is_write_protected(Path("C:/repo/conference.md"))


def test_unc_and_extended_length_paths_protected():
    assert is_write_protected(Path(r"\\server\share\file.txt"))
    assert is_write_protected(Path(r"\\?\C:\repo\x.txt"))


def test_traversal_segments_protected():
    assert is_write_protected(Path("/repo/../etc/passwd"))


# ---- A4: Windows path guard completion ----

def test_trailing_dot_and_space_names_protected():
    """Windows strips trailing dots/spaces — Write("settings.json. ") hits
    the protected settings.json name."""
    assert is_write_protected(Path("settings.json. "))
    assert is_write_protected(Path("settings.json."))
    assert is_write_protected(Path("C:/repo/.env."))
    assert is_write_protected(Path("C:/repo/.bashrc "))
    assert is_write_protected(Path("C:/repo/CON. "))
    assert not is_write_protected(Path("C:/repo/main.py."))


def test_ntfs_ads_streams_protected():
    """A colon past the drive letter is an alternate data stream."""
    assert is_write_protected(Path("settings.json:stream"))
    assert is_write_protected(Path("C:/repo/x.txt:hidden"))
    assert not is_write_protected(Path("C:/repo/x.txt"))  # drive colon at index 1 is fine


def test_iis_virtual_segments_protected():
    assert is_write_protected(Path("C:/inetpub/wwwroot/@SSL@/x"))
    assert is_write_protected(Path(r"C:\inetpub\wwwroot\@SSL@\file.txt"))
    assert is_write_protected(Path(r"\\server\DavWWWRoot\share\file.txt"))


def test_ip_unc_share_protected():
    assert is_write_protected(Path(r"\\192.168.1.5\share\file.txt"))
    assert is_write_protected(Path(r"\\10.0.0.1\x\y.txt"))


def test_device_prefix_paths_protected():
    assert is_write_protected(Path(r"\\.\C:\repo\x.txt"))
    assert is_write_protected(Path(r"\\?\C:\repo\x.txt"))


# ---- CC-05: case-insensitive path comparison ----

def test_case_insensitive_protected_paths():
    """macOS/Windows are case-insensitive: .GIT / settings.LOCAL.json /
    .SSH must be protected just like their lowercase spellings."""
    assert is_write_protected(Path("/repo/.GIT/config"))
    assert is_write_protected(Path("/repo/.Git/objects/x"))
    assert is_write_protected(Path("/home/u/.SSH/id_rsa"))
    assert is_write_protected(Path("/repo/settings.LOCAL.json"))
    assert is_write_protected(Path("/home/u/.Bashrc"))
    assert is_write_protected(Path("/repo/.VSCODE/settings.json"))
    assert is_write_protected(Path("/repo/.Env"))
    assert not is_write_protected(Path("/repo/GIT_README.md"))


def test_case_insensitive_sensitive_reads():
    assert is_sensitive_path(Path("/repo/.ENV"))
    assert is_sensitive_path(Path("/repo/SETTINGS.LOCAL.JSON"))


# ---- CC-06: symlink dual-path candidates ----

def test_resolve_candidates_lexical_and_real(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not permitted on this system")
    target = link / "x.txt"
    cands = resolve_candidates(target)
    assert cands == [link / "x.txt", real / "x.txt"]


def test_resolve_candidates_dedup_without_symlink(tmp_path):
    cands = resolve_candidates(tmp_path / "x.txt")
    assert len(cands) == 1
    assert cands[0] == (tmp_path / "x.txt").resolve()
