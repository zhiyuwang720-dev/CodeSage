"""spec §6 test_importer: 本地 fixture 仓库 clone 成功, 持久化目录结构正确。"""
from app.services.pr_review.diff_importer import import_github_pr
from app.services.pr_review.paths import diff_path, repo_dir


def test_local_fixture_clone_to_persistent_dir(fixture_repo):
    imported = import_github_pr(
        "https://github.com/acme/fixture/pull/7",
        clone_source=str(fixture_repo.path),
        head_ref="feature/mul",
    )

    assert imported.pr_key == "acme__fixture#7"
    assert imported.repo == "acme/fixture"
    assert imported.pr_number == 7

    dest = repo_dir(imported.pr_key)
    assert (dest / ".git").is_dir(), "clone 到持久化目录"
    assert (dest / "utils.py").is_file()
    assert (dest / "service.py").is_file()

    assert imported.head_sha == fixture_repo.head_sha
    assert imported.base_sha
    assert imported.diff_text, "统一 diff 已提取"


def test_clone_is_cached(fixture_repo):
    """同一 pr_key 二次导入复用已有 clone(不重复 clone)。"""
    kwargs = dict(clone_source=str(fixture_repo.path), head_ref="feature/mul")
    first = import_github_pr("https://github.com/acme/fixture/pull/7", **kwargs)
    second = import_github_pr("https://github.com/acme/fixture/pull/7", **kwargs)
    assert first.repo_dir == second.repo_dir
    assert diff_path(first.pr_key).is_file()
