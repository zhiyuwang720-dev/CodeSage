"""spec §6 test_context_git_history: git log 提取提交/作者/意图摘要正确。"""
from app.services.pr_review.context_collector import collect_git_history


def test_git_history_basic(fixture_repo):
    commits = collect_git_history(fixture_repo.path, fixture_repo.base_sha, fixture_repo.head_sha)
    assert len(commits) == 1, "base..head 区间内只有一个提交"
    c = commits[0]
    assert c.sha == fixture_repo.head_sha
    assert c.author == "tester"
    assert c.message == "feat(utils): add mul helper"
    assert c.is_merge is False


def test_git_history_multiple_commits(fixture_repo):
    import subprocess

    repo = fixture_repo.path
    subprocess.run(["git", "-C", str(repo), "checkout", "feature/mul"], check=True, capture_output=True)
    (repo / "EXTRA.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "chore: extra"], check=True, capture_output=True
    )
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    commits = collect_git_history(repo, fixture_repo.base_sha, head)
    messages = [c.message for c in commits]
    assert "feat(utils): add mul helper" in messages
    assert "chore: extra" in messages
    # 最新提交在前(git log 顺序)
    assert messages[0] == "chore: extra"


def test_git_history_empty_without_sha(fixture_repo):
    assert collect_git_history(fixture_repo.path, None, fixture_repo.head_sha) == []
