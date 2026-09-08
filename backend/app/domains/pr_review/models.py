"""pr_review 契约(阶段 01 §3.3.4)。

ReviewContext 是 Orchestrator 分发的输入(阶段 02 消费);
ReviewComment 是对外输出契约(benchmark 注入格式 {path, line, body, severity, category})。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GitCommitInfo(BaseModel):
    """git 历史(§3.3 维度一): base..head 区间提交,识别重命名/移动与变更意图。"""

    sha: str
    author: str
    message: str
    is_merge: bool = False


class RelatedFile(BaseModel):
    """相关文件(§3.3 维度二): 确定性提取,按引用强度排序,受预算约束。"""

    path: str
    reason: str  # import | caller | test
    strength: int  # 越大越强: import=3 caller=2 test=1
    size_bytes: int = 0
    content: str | None = None  # 预算允许时填充


class ReviewContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str
    pr_number: int | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    diff_text: str = ""
    diff_only: bool = False  # diff-only 模式: git_history/related_files 为空数组
    git_history: list[GitCommitInfo] = Field(default_factory=list)
    related_files: list[RelatedFile] = Field(default_factory=list)
    ci_status: dict | None = None  # CI 不可用时为 None,不阻塞审查
    command: str = "review"
    options: dict = Field(default_factory=dict)
    user_context: str | None = None  # 用户注入上下文(diff-only 模式的上下文替代)
    source_dir: str | None = None  # 本地仓库目录(上下文收集的工作区)
    pr_key: str | None = None  # 持久化键, 产物 .auditai/context/<pr_key>.json


class ReviewComment(BaseModel):
    path: str
    line: int
    body: str
    severity: str | None = None
    category: str | None = None


class ImportedPr(BaseModel):
    """PR 导入产物: 本地目录 + 统一 diff + 关键 sha。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    pr_key: str
    repo: str
    pr_number: int | None = None
    repo_dir: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    diff_text: str
    diff_only: bool = False
