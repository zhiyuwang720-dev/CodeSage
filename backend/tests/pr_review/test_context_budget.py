"""spec §6 test_context_budget: 相关文件超预算 → 按引用强度裁剪, 不崩溃。"""
from app.services.pr_review.context_collector import collect_related_files
from app.services.pr_review.diff_importer import extract_diff


def test_budget_trim_no_crash(fixture_repo):
    diff = extract_diff(fixture_repo.path, fixture_repo.base_sha, fixture_repo.head_sha)
    related = collect_related_files(fixture_repo.path, diff, budget_bytes=10)
    # 超小预算: 至多保留引用强度最高的 1 个文件(保底), 其余裁剪
    assert len(related) <= 1
    for r in related:
        assert r.content is None, "超预算文件不读内容"


def test_budget_admits_files_within_limit(fixture_repo):
    diff = extract_diff(fixture_repo.path, fixture_repo.base_sha, fixture_repo.head_sha)
    related = collect_related_files(fixture_repo.path, diff, budget_bytes=1_000_000)
    assert len(related) >= 2
    assert all(r.content for r in related)


def test_max_files_cap(tmp_path, fixture_repo):
    """max_files 上限: 造一堆相关文件, 验证截断。"""
    repo = fixture_repo.path
    for i in range(6):
        (repo / f"caller_{i}.py").write_text(
            "import utils\n" + f"\n\ndef use_{i}():\n    return utils.add(1, 2)\n", encoding="utf-8"
        )
    import subprocess

    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "feat: more callers"],
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    diff = extract_diff(repo, fixture_repo.base_sha, head)
    related = collect_related_files(repo, diff, max_files=3)
    assert len(related) <= 3, "max_files 截断生效"
