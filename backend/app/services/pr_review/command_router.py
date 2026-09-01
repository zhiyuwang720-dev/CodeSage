"""command_router: 命令分发 + 统一审查入口(阶段 01 §3.1 / 阶段 02 §3.2 升级)。

命令 dict 思路来自 pr-agent agent/pr_agent.py:23-45 的 command2class(GitHub MIT)。
统一入口: 导入 → 上下文收集 → 引擎分发 → 结果落盘。
- engine=rules(默认, 全离线): 确定性规则层 + 综合层, 不依赖 LLM;
- engine=runtime: ReviewOrchestrator 三视角星型编排(真实 LLM), 见 run_review_pipeline_async。
"""
from __future__ import annotations

import inspect
import json
import uuid
from typing import Any

from .context_collector import build_review_context
from .git_providers import provider_for_input
from .models import ImportedPr, ReviewComment
from .orchestrator import ReviewOrchestrator
from .paths import review_path
from .plain_diff_importer import import_plain_diff
from .rules import run_rules
from .synthesizer import finding_to_comment, synthesize


class ReviewResult:
    def __init__(
        self,
        review_id: str,
        pr_key: str,
        status: str,
        comments: list[ReviewComment] | None = None,
        context_path: str | None = None,
        meta: dict | None = None,
    ):
        self.review_id = review_id
        self.pr_key = pr_key
        self.status = status
        self.comments = comments or []
        self.context_path = context_path
        self.meta = meta or {}

    def to_dict(self) -> dict:
        return {
            "review_id": self.review_id,
            "pr_key": self.pr_key,
            "status": self.status,
            "comments": [c.model_dump() for c in self.comments],
            "context_path": self.context_path,
            "meta": self.meta,
        }

    def persist(self) -> None:
        review_path(self.review_id).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ── 命令注册表(pr-agent command2class 思路, 改名) ─────────────────
COMMAND_HANDLERS: dict[str, str] = {
    "review": "rules_reviewer",
    "describe": "rules_reviewer",
    "ask_line": "rules_reviewer",
}

REVIEWER_FUNCS = {
    "rules_reviewer": run_rules,
}


def resolve_command(command: str | None) -> str:
    """未知命令回落 review(pr-agent 行为)。"""
    command = (command or "review").strip().lower()
    return command if command in COMMAND_HANDLERS else "review"


def _import_and_collect(
    *,
    pr_url: str | None = None,
    diff_text: str | None = None,
    user_context: str | None = None,
    command: str,
    options: dict,
    provider=None,
) -> tuple[ImportedPr, Any]:
    """公共前置: 导入 → 上下文收集(上下文先于分发, spec 02 §2 原则④)。全部同步。"""
    if pr_url:
        from .diff_importer import import_github_pr

        provider = provider or provider_for_input(pr_url=pr_url)
        imported = import_github_pr(
            pr_url,
            clone_source=options.get("clone_source"),
            head_ref=options.get("head_ref"),
            base_ref=options.get("base_ref", "origin/main"),
            token=options.get("github_token"),
        )
    elif diff_text is not None:
        provider = provider or provider_for_input(diff_text=diff_text)
        imported = import_plain_diff(diff_text, repo=options.get("repo"), pr_number=options.get("pr_number"))
    else:
        raise ValueError("需要 pr_url 或 diff_text 之一")

    ctx = build_review_context(
        imported,
        provider=provider,
        user_context=user_context,
        command=command,
        options=options,
    )
    return imported, ctx


def run_review_pipeline(
    *,
    pr_url: str | None = None,
    diff_text: str | None = None,
    user_context: str | None = None,
    command: str | None = "review",
    options: dict | None = None,
    provider=None,
) -> ReviewResult:
    """同步入口(全离线): 默认 rules 引擎; engine=runtime 时请用异步入口。"""
    options = options or {}
    engine = str(options.get("engine", "rules"))
    if engine != "rules":
        raise ValueError("engine=runtime 需要 await run_review_pipeline_async(...)")
    command = resolve_command(command)
    imported, ctx = _import_and_collect(
        pr_url=pr_url,
        diff_text=diff_text,
        user_context=user_context,
        command=command,
        options=options,
        provider=provider,
    )
    reviewer = REVIEWER_FUNCS[COMMAND_HANDLERS[command]]
    raw_findings = [f.model_dump() for f in reviewer(ctx.diff_text)]
    synthesis = synthesize(
        raw_findings,
        diff_text=ctx.diff_text,
        max_comments=int(options.get("max_comments", 10)),
        min_severity=str(options.get("min_severity", "high")),
    )
    comments = [ReviewComment(**finding_to_comment(f)) for f in synthesis.comments]
    result = ReviewResult(
        review_id=f"{imported.pr_key}-{uuid.uuid4().hex[:8]}",
        pr_key=imported.pr_key,
        status="completed",
        comments=comments,
        context_path=str(ctx.pr_key),
        meta={
            "engine": "rules",
            "deduped_away": synthesis.deduped_away,
            "rejected_off_diff": synthesis.rejected_off_diff,
        },
    )
    result.persist()
    return result


async def run_review_pipeline_async(
    *,
    pr_url: str | None = None,
    diff_text: str | None = None,
    user_context: str | None = None,
    command: str | None = "review",
    options: dict | None = None,
    provider=None,
    event_sink=None,
) -> ReviewResult:
    """异步入口: engine=runtime 走三视角星型编排; 其他引擎回落同步路径。

    event_sink 是运行时对象(事件回调), 必须独立传参而非塞进 options——
    options 会被 build_review_context 原样持久化(model_dump_json), 非 JSON 类型会崩。
    dispatcher / session_factory 同理, 会在本函数开头从 options 抽出后单独使用。
    """
    options = dict(options or {})
    # options 会被 build_review_context 原样持久化(model_dump_json)并落盘,
    # 运行时对象(dispatcher / session_factory / event_sink)不是 JSON 类型, 混入会崩。
    # 这里把注入通道从 options 抽出为局部变量, 持久化的 options 保持纯净数据。
    dispatcher = options.pop("dispatcher", None)
    session_factory = options.pop("session_factory", None)
    event_sink = event_sink if event_sink is not None else options.pop("event_sink", None)
    streaming = bool(options.pop("streaming", False))
    engine = str(options.get("engine", "rules"))
    if engine != "runtime":
        return run_review_pipeline(
            pr_url=pr_url,
            diff_text=diff_text,
            user_context=user_context,
            command=command,
            options=options,
            provider=provider,
        )

    command = resolve_command(command)
    imported, ctx = _import_and_collect(
        pr_url=pr_url,
        diff_text=diff_text,
        user_context=user_context,
        command=command,
        options=options,
        provider=provider,
    )

    # 头部元信息: 项目/模型等已知数据先行, 供 TUI 启动即显示(不分视角, 走原始 sink)。
    if event_sink is not None:
        from app.core.config import settings as _cfg

        meta_event = {
            "type": "meta",
            "project_id": ctx.pr_key or ctx.repo,
            "repo": ctx.repo,
            "pr_number": ctx.pr_number,
            "engine": "runtime",
            "model": getattr(_cfg, "LLM_MODEL", None),
        }
        maybe_awaitable = event_sink(meta_event)
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable

    if dispatcher is None:
        from app.services.agent.tools.shared_catalog import build_shared_agent_tool_catalog
        from app.services.llm.service import LLMService

        from .runtime_dispatcher import RuntimePerspectiveDispatcher

        project_root = ctx.source_dir or "."
        dispatcher = RuntimePerspectiveDispatcher(
            llm_service=LLMService(),
            tools=build_shared_agent_tool_catalog(project_root=project_root),
            project_id=ctx.pr_key or ctx.repo,
            task_id=options.get("task_id"),
            session_factory=session_factory,
            max_turns=options.get("max_turns"),
            event_sink=event_sink,
        )
    orchestrator = ReviewOrchestrator(
        dispatcher,
        min_severity=str(options.get("min_severity", "high")),
        max_comments=int(options.get("max_comments", 10)),
    )
    # streaming=True: 用户拍板真流式 —— 临时关掉网关兼容开关(阻塞合成流→逐 token SSE)。
    # bridge.stream_complete 每轮调用时读取该开关(bridge.py), 开跑前改、跑完 finally 还原。
    _prev_disable_streaming = None
    if streaming:
        from app.core.config import settings as _cfg

        _prev_disable_streaming = _cfg.LLM_DISABLE_STREAMING
        _cfg.LLM_DISABLE_STREAMING = False
    try:
        review = await orchestrator.run(ctx)
    finally:
        if _prev_disable_streaming is not None:
            from app.core.config import settings as _cfg

            _cfg.LLM_DISABLE_STREAMING = _prev_disable_streaming
    comments = [ReviewComment(**c) for c in review.benchmark_comments]
    result = ReviewResult(
        review_id=f"{imported.pr_key}-{uuid.uuid4().hex[:8]}",
        pr_key=imported.pr_key,
        status="completed",
        comments=comments,
        context_path=str(ctx.pr_key),
        meta={"engine": "runtime", **review.to_result_dict()},
    )
    result.persist()
    return result
