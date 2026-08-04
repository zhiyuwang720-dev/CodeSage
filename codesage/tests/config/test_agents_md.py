"""AGENTS.md discovery tests."""

from codesage.config import find_git_root, get_project_instruction_files


def make_repo(tmp_path, name="repo"):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    return root


def test_find_git_root_from_subdir(tmp_path):
    root = make_repo(tmp_path)
    (root / "a" / "b").mkdir(parents=True)
    assert find_git_root(root / "a" / "b") == root


def test_find_git_root_none(tmp_path):
    assert find_git_root(tmp_path) is None


def test_empty_without_git(tmp_path):
    assert get_project_instruction_files(cwd=tmp_path) == []


def test_collects_outer_to_inner(tmp_path):
    root = make_repo(tmp_path)
    (root / "sub").mkdir()
    (root / "AGENTS.md").write_text("outer")
    (root / "sub" / "AGENTS.md").write_text("inner")
    files = get_project_instruction_files(cwd=root / "sub")
    assert [f.parent.name for f in files] == ["repo", "sub"]


def test_override_replaces_plain_at_same_level(tmp_path):
    root = make_repo(tmp_path)
    (root / "AGENTS.md").write_text("plain")
    (root / "AGENTS.override.md").write_text("override")
    files = get_project_instruction_files(cwd=root)
    assert len(files) == 1
    assert files[0].name == "AGENTS.override.md"


def test_override_only_at_outer_level(tmp_path):
    root = make_repo(tmp_path)
    (root / "sub").mkdir()
    (root / "AGENTS.md").write_text("outer")
    (root / "sub" / "AGENTS.override.md").write_text("inner-override")
    files = get_project_instruction_files(cwd=root / "sub")
    assert [f.name for f in files] == ["AGENTS.md", "AGENTS.override.md"]


def test_subdir_without_own_instructions_inherits(tmp_path):
    root = make_repo(tmp_path)
    (root / "deep" / "deeper").mkdir(parents=True)
    (root / "AGENTS.md").write_text("only-outer")
    files = get_project_instruction_files(cwd=root / "deep" / "deeper")
    assert [f.parent.name for f in files] == ["repo"]
