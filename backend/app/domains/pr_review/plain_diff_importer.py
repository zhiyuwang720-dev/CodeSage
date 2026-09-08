"""plain_diff_importer: 纯 diff 输入落盘(阶段 01 §3.1, benchmark 主通道)。

模式参考 pr-agent PlainDiffGitProvider(GitHub MIT, plain_diff_provider.py 的
diff 即数据思路): diff 文本来自 stdin/文件, repo/pr_number 可选注入。
clone_source 可选: 给出时克隆源码到 repos/, 使审查工具能读/搜被改文件;
克隆失败(私有仓库/网络)自动降级 diff_only=True, 不阻塞审查。
"""
from __future__ import annotations

import hashlib
import logging

from .git_providers import clone_repo
from .models import ImportedPr
from .paths import diff_path, repo_dir

logger = logging.getLogger(__name__)


def pr_key_for(repo: str | None, pr_number: int | None, diff_text: str | None = None) -> str:
    """持久化键: repo#pr 优先; 纯 diff 用内容哈希(同一 diff 复用同一键)。"""
    if repo and pr_number is not None:
        return f"{repo.replace('/', '__')}#{pr_number}"
    digest = hashlib.sha256((diff_text or "").encode("utf-8")).hexdigest()[:12]
    return f"plain-{digest}"


def import_plain_diff(
    diff_text: str,
    repo: str | None = None,
    pr_number: int | None = None,
    clone_source: str | None = None,
) -> ImportedPr:
    """纯 diff 导入; clone_source 给出时尝试克隆源码供工具访问。"""
    if not diff_text.strip():
        raise ValueError("diff 文本为空")
    pr_key = pr_key_for(repo, pr_number, diff_text)
    diff_path(pr_key).write_text(diff_text, encoding="utf-8")

    repo_dir_path = None
    diff_only = True
    if clone_source:
        dest = repo_dir(pr_key)
        try:
            if not (dest / ".git").exists():
                clone_repo(clone_source, dest)
            repo_dir_path = str(dest)
            diff_only = False
            logger.info(
                "plain-diff 已克隆源码 %s → %s", clone_source, dest
            )
        except Exception as exc:  # 私有仓库/网络失败: 降级 diff-only, 审查不中断
            logger.warning(
                "plain-diff 克隆源码失败(%s), 降级 diff-only: %s", clone_source, exc
            )

    return ImportedPr(
        pr_key=pr_key,
        repo=repo or "plain-diff",
        pr_number=pr_number,
        repo_dir=repo_dir_path,
        diff_text=diff_text,
        diff_only=diff_only,
    )
