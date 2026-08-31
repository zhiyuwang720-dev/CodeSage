"""spec §6 test_context_related_files: diff 引用分析命中调用方与测试文件。"""
from app.services.pr_review.context_collector import collect_related_files
from app.services.pr_review.diff_importer import extract_diff


def test_related_files_includes_caller_and_test(fixture_repo):
    diff = extract_diff(fixture_repo.path, fixture_repo.base_sha, fixture_repo.head_sha)
    related = collect_related_files(fixture_repo.path, diff)
    by_path = {r.path: r for r in related}

    # 修改 utils.py → 其调用方与测试文件应入选
    assert "service.py" in by_path, "被改模块的调用方"
    assert by_path["service.py"].reason == "caller"
    assert by_path["service.py"].strength >= 2

    assert "tests/test_utils.py" in by_path, "被改模块的测试文件"
    assert by_path["tests/test_utils.py"].reason == "test"

    # 被改文件本身不属于"相关文件"(已在 diff 里)
    assert "utils.py" not in by_path


def test_related_files_sorted_by_strength(fixture_repo):
    diff = extract_diff(fixture_repo.path, fixture_repo.base_sha, fixture_repo.head_sha)
    related = collect_related_files(fixture_repo.path, diff)
    strengths = [r.strength for r in related]
    assert strengths == sorted(strengths, reverse=True), "按引用强度降序"


def test_related_files_content_filled_under_budget(fixture_repo):
    diff = extract_diff(fixture_repo.path, fixture_repo.base_sha, fixture_repo.head_sha)
    related = collect_related_files(fixture_repo.path, diff, budget_bytes=60_000)
    assert all(r.content for r in related), "预算充足时填充文件内容"


def test_diff_only_no_repo_returns_empty():
    related = collect_related_files(None, "+import os\n")
    assert related == []
