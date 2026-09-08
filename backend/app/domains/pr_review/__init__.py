"""PR 审查输入层(阶段 01): 事件解析 → diff 提取 → 上下文收集 → 统一审查入口。

来源参考:
- pr-agent MIT 的入口分层思路(命令分发/幂等并发/webhook BackgroundTasks)
- AutoCVE 持久化目录约定(zip_storage.get_project_persistent_source_path)
本层不含任何审查逻辑;审查引擎在阶段 02 接入(当前为占位审查器)。
"""
from .models import (
    GitCommitInfo,
    ImportedPr,
    RelatedFile,
    ReviewComment,
    ReviewContext,
)
from .paths import (
    context_path,
    diff_path,
    pr_data_root,
    repo_dir,
    review_path,
)

__all__ = [
    "GitCommitInfo",
    "ImportedPr",
    "RelatedFile",
    "ReviewComment",
    "ReviewContext",
    "pr_data_root",
    "repo_dir",
    "diff_path",
    "context_path",
    "review_path",
]
