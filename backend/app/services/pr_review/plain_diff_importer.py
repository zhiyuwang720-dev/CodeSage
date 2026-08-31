"""plain_diff_importer: 纯 diff 输入落盘, 不 clone(阶段 01 §3.1, benchmark 主通道)。

模式参考 pr-agent PlainDiffGitProvider(GitHub MIT, plain_diff_provider.py 的
diff 即数据思路): diff 文本来自 stdin/文件, repo/pr_number 可选注入。
"""
from __future__ import annotations

import hashlib

from .models import ImportedPr
from .paths import diff_path


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
) -> ImportedPr:
    if not diff_text.strip():
        raise ValueError("diff 文本为空")
    pr_key = pr_key_for(repo, pr_number, diff_text)
    diff_path(pr_key).write_text(diff_text, encoding="utf-8")
    return ImportedPr(
        pr_key=pr_key,
        repo=repo or "plain-diff",
        pr_number=pr_number,
        repo_dir=None,
        diff_text=diff_text,
        diff_only=True,
    )
