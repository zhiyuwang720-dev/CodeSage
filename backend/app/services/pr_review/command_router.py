"""command_router: 命令分发 + 统一审查入口(阶段 01 §3.1/§4.5)。

命令 dict 思路来自 pr-agent agent/pr_agent.py:23-45 的 command2class(GitHub MIT),
按本仓库语义改名为 review/describe/ask_line。
统一入口 run_review_pipeline: 任意输入模式 → 导入 → 上下文收集 → 审查命令。
阶段 01 审查器为占位实现(返回空评论, 验证链路); 阶段 02 在此接 Orchestrator。
"""
from __future__ import annotations

import json
import uuid

from .context_collector import build_review_context
from .git_providers import provider_for_input
from .models import ImportedPr, ReviewComment, ReviewContext
from .paths import review_path
from .plain_diff_importer import import_plain_diff


class ReviewResult:
    def __init__(
        self,
        review_id: str,
        pr_key: str,
        status: str,
        comments: list[ReviewComment] | None = None,
        context_path: str | None = None,
    ):
        self.review_id = review_id
        self.pr_key = pr_key
        self.status = status
        self.comments = comments or []
        self.context_path = context_path

    def to_dict(self) -> dict:
        return {
            "review_id": self.review_id,
            "pr_key": self.pr_key,
            "status": self.status,
            "comments": [c.model_dump() for c in self.comments],
            "context_path": self.context_path,
        }

    def persist(self) -> None:
        review_path(self.review_id).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ── 命令注册表(pr-agent command2class 思路, 改名) ─────────────────
COMMAND_HANDLERS: dict[str, str] = {
    "review": "placeholder_reviewer",
    "describe": "placeholder_reviewer",
    "ask_line": "placeholder_reviewer",
}


def resolve_command(command: str | None) -> str:
    """未知命令回落 review(pr-agent 行为); 注册表外的命令显式拒绝。"""
    command = (command or "review").strip().lower()
    if command in COMMAND_HANDLERS:
        return command
    return "review"


def placeholder_reviewer(ctx: ReviewContext) -> list[ReviewComment]:
    """占位审查器(阶段 02 接 Orchestrator/三 Agent): 输入契约不变, 评论为空。"""
    return []


REVIEWER_FUNCS = {
    "placeholder_reviewer": placeholder_reviewer,
}


def run_review_pipeline(
    *,
    pr_url: str | None = None,
    diff_text: str | None = None,
    user_context: str | None = None,
    command: str | None = "review",
    options: dict | None = None,
    provider=None,
) -> ReviewResult:
    """统一审查入口: 导入 → 上下文收集 → 命令分发 → 结果落盘。

    - diff-only: provider 缺省按 diff_text 构造, 不收集自动上下文
    - pr_url:    GitHub provider(可注入 fetcher); clone/上下文收集可离线复用缓存
    """
    options = options or {}
    command = resolve_command(command)
    if pr_url:
        from .diff_importer import import_github_pr  # 延迟导入: CLI 纯 diff 不触碰 git clone

        provider = provider or provider_for_input(pr_url=pr_url)
        clone_source = options.get("clone_source")
        head_ref = options.get("head_ref")
        base_ref = options.get("base_ref", "origin/main")
        imported: ImportedPr = import_github_pr(
            pr_url,
            clone_source=clone_source,
            head_ref=head_ref,
            base_ref=base_ref,
            token=options.get("github_token"),
        )
    elif diff_text is not None:
        provider = provider or provider_for_input(diff_text=diff_text)
        imported = import_plain_diff(
            diff_text,
            repo=options.get("repo"),
            pr_number=options.get("pr_number"),
        )
    else:
        raise ValueError("需要 pr_url 或 diff_text 之一")

    ctx = build_review_context(
        imported,
        provider=provider,
        user_context=user_context,
        command=command,
        options=options,
    )
    reviewer = REVIEWER_FUNCS[COMMAND_HANDLERS[command]]
    comments = reviewer(ctx)
    result = ReviewResult(
        review_id=f"{imported.pr_key}-{uuid.uuid4().hex[:8]}",
        pr_key=imported.pr_key,
        status="completed",
        comments=comments,
        context_path=str(ctx.pr_key),
    )
    result.persist()
    return result
