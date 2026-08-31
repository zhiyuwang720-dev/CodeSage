"""spec §6 test_diff_extraction: 生成的 diff 与 git diff 输出一致。"""
import subprocess

from app.services.pr_review.diff_importer import extract_diff, resolve_sha


def test_extract_diff_matches_git(fixture_repo):
    repo = fixture_repo.path
    diff = extract_diff(repo, fixture_repo.base_sha, fixture_repo.head_sha)

    direct = subprocess.run(
        ["git", "-C", str(repo), "diff", f"{fixture_repo.base_sha}...{fixture_repo.head_sha}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert diff == direct.stdout


def test_extract_diff_contains_changed_file(fixture_repo):
    diff = extract_diff(fixture_repo.path, fixture_repo.base_sha, fixture_repo.head_sha)
    assert "b/utils.py" in diff
    assert "+def mul" in diff
    assert "b/service.py" not in diff, "调用方未被修改, 不应出现在 diff 中"


def test_resolve_sha(fixture_repo):
    # fixture 末尾切回 main, HEAD 已变; 按分支名解析 feature head
    assert resolve_sha(fixture_repo.path, "feature/mul") == fixture_repo.head_sha
