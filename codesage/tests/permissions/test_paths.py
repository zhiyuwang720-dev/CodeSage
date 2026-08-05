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
