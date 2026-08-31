"""diff_importer: clone PR 分支到持久化目录 + 本地 git diff 提取(阶段 01 §4.1-4.2)。

私有仓库走 git_ssh_service / token 的配置与 AutoCVE 既有约定一致; 本地路径与
file:// 直接可 clone(测试 fixture 同通道)。
"""
from __future__ import annotations

from .git_providers import GitHubRepoInfo, clone_repo, parse_github_pr_url, run_git
from .models import ImportedPr
from .paths import diff_path, repo_dir
from .plain_diff_importer import pr_key_for


def resolve_sha(repo_dir_path, ref: str) -> str:
    return run_git(repo_dir_path, "rev-parse", ref).strip()


def extract_diff(repo_dir_path, base_sha: str, head_sha: str) -> str:
    """统一 diff(base...head)。输出与 `git diff base...head` 一致(test_diff_extraction)。"""
    return run_git(repo_dir_path, "diff", f"{base_sha}...{head_sha}")


def import_github_pr(
    pr_url: str,
    clone_source: str | None = None,
    base_ref: str = "origin/main",
    head_ref: str | None = None,
    token: str | None = None,
    checkout_base: bool = True,
) -> ImportedPr:
    """导入 GitHub PR。

    - clone_source: 覆盖 clone 来源(测试传本地路径/私有 remote); 缺省按 GitHub URL 推导
    - head_ref: 分支名; 缺省用 FETCH_HEAD 流程(远端 PR head)
    - diff: 本地 `git diff base...head`; base 取 merge-base(base_ref, head)
    """
    info = parse_github_pr_url(pr_url)
    pr_key = pr_key_for(f"{info.owner}/{info.repo}", info.number)
    dest = repo_dir(pr_key)
    source = clone_source or pr_url
    if not (dest / ".git").exists():
        clone_repo(source, dest)
    if head_ref:
        run_git(dest, "fetch", "origin", head_ref, check=False)
        head_sha = resolve_sha(dest, "FETCH_HEAD")
    else:
        run_git(dest, "fetch", "origin", check=False)
        head_sha = resolve_sha(dest, "HEAD")
    base_sha = resolve_sha(dest, base_ref)
    if checkout_base:
        base_used = run_git(dest, "merge-base", base_sha, head_sha).strip()
    else:
        base_used = base_sha
    diff_text = extract_diff(dest, base_used, head_sha)
    diff_path(pr_key).write_text(diff_text, encoding="utf-8")
    return ImportedPr(
        pr_key=pr_key,
        repo=f"{info.owner}/{info.repo}",
        pr_number=info.number,
        repo_dir=str(dest),
        base_sha=base_used,
        head_sha=head_sha,
        diff_text=diff_text,
    )


def import_local_repo(
    repo_path,
    base_ref: str,
    head_ref: str,
    repo_name: str | None = None,
    pr_number: int | None = None,
) -> ImportedPr:
    """本地仓库导入(CI fixture / 离线排查通道): 直接对现有工作区算 diff, 不 clone。"""
    repo_path = str(repo_path)
    base_sha = resolve_sha(repo_path, base_ref)
    head_sha = resolve_sha(repo_path, head_ref)
    merge_base = run_git(repo_path, "merge-base", base_sha, head_sha).strip()
    diff_text = extract_diff(repo_path, merge_base, head_sha)
    key_repo = repo_name or repo_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    pr_key = pr_key_for(key_repo, pr_number)
    diff_path(pr_key).write_text(diff_text, encoding="utf-8")
    return ImportedPr(
        pr_key=pr_key,
        repo=key_repo,
        pr_number=pr_number,
        repo_dir=repo_path,
        base_sha=merge_base,
        head_sha=head_sha,
        diff_text=diff_text,
    )
