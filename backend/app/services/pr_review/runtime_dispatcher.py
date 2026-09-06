"""运行时分发器(阶段 02 §3.2): 视角 → RuntimeBridge(独立 session)。

生产路径: 每个视角一个 bridge 实例(agent_type=review:<视角>), 注册表内部挂
FinalizeReview 终点工具并按权限矩阵裁剪工具集; TaskHandoff 为唯一回传通道。
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from app.services.agent.prompts.review_prompts import build_followup_prompt
from app.services.pr_review.orchestrator import PERSPECTIVE_PROMPTS, TOOL_MATRICES

REVIEW_FINALIZER_PROMPTS = [
    "如果审查已经充分完成：调用 FinalizeReview 提交结构化评论集(findings+summary)；"
    "或输出可解析的 {\"findings\": [...], \"summary\": \"...\"} JSON。\n"
    "注意：评论必须落在 diff 新增行(head 行号)；没有可报告问题时提交空 findings 并在 summary 说明范围。\n"
    "当前为最终提交阶段：除 FinalizeReview 外，其他工具(Read/Bash/Skill 等)已全部关闭，请勿再调用。"
]


class PerspectiveResumeIncompleteError(Exception):
    """视角续跑/重跑未产出真实 payload(_default_fallback_payload 兜底产出)。

    12-P2.1: payload 自标 requires_retry / runtime_completion_mode=incomplete —— 明确
    "未完成需重试"。不得当作完成视角发 perspective_done(否则 stage 被置 completed、
    orchestrator 综合空结果 → 任务假完成)。orchestrator 的 return_exceptions=True 会
    捕获此异常并把该视角记为失败, review:* stage 不置 completed, 任务可再次「继续」。
    """


def tag_event_sink(event_sink, perspective: str):
    """把视角名打进事件 dict 后转发给内层 sink。

    三视角在 asyncio.gather 下并发, 事件流互相交织; 外层 sink(如 CLI 进度输出)
    靠 perspective 区分进度归属。内层可为同步或异步(event_sink 契约见
    query_loop._emit_event: Callable[[dict], Any], 返回值可 await 也可同步)。
    内层为 None 时返回 None(保持"未配置 sink"的既有空转行为)。
    """
    if event_sink is None:
        return None

    async def sink(event: dict[str, Any]) -> None:
        tagged = dict(event)
        tagged["perspective"] = perspective
        result = event_sink(tagged)
        if inspect.isawaitable(result):
            await result

    return sink


@dataclass(frozen=True)
class ReviewPerspectiveSpec:
    """视角运行规格(引擎 review:* 单语义视角的 runtime spec 落点)。"""

    perspective: str
    agent_type: str
    system_prompt: str
    tool_allowlist: set[str]


def build_review_perspective_spec(perspective: str) -> ReviewPerspectiveSpec:
    if perspective not in PERSPECTIVE_PROMPTS:
        raise ValueError(f"未知审查视角: {perspective!r}")
    return ReviewPerspectiveSpec(
        perspective=perspective,
        agent_type=f"review:{perspective}",
        system_prompt=PERSPECTIVE_PROMPTS[perspective],
        tool_allowlist=TOOL_MATRICES[perspective],
    )


def build_review_recon_payload(ctx: Any) -> dict[str, Any]:
    """ReviewContext → 运行时 recon_payload(上下文先于分发, §2 原则④)。"""
    related = [
        {"path": f.path, "reason": f.reason, "content": f.content}
        for f in (getattr(ctx, "related_files", None) or [])
    ]
    history = [
        {"sha": c.sha, "author": c.author, "message": c.message}
        for c in (getattr(ctx, "git_history", None) or [])
    ]
    return {
        "pr_key": getattr(ctx, "pr_key", None),
        "repo": ctx.repo,
        "pr_number": ctx.pr_number,
        "diff_text": ctx.diff_text,
        "related_files": related,
        "git_history": history,
        "ci_status": ctx.ci_status,
        "user_context": ctx.user_context,
    }


class RuntimePerspectiveDispatcher:
    """真运行时分发器: 视角 → 独立 session 的完整 ReAct 循环。"""

    def __init__(
        self,
        *,
        llm_service,
        tools: list[Any],
        project_id: str,
        task_id: str | None = None,
        user_id: str | None = None,
        session_factory=None,
        event_sink=None,
        max_turns: int | None = None,
    ):
        self._llm_service = llm_service
        self._tools = tools
        self._project_id = project_id
        self._task_id = task_id
        self._user_id = user_id
        self._session_factory = session_factory
        self._event_sink = event_sink
        self._max_turns = max_turns
        self._session_ids: dict[str, str] = {}

    async def __call__(
        self,
        perspective: str,
        ctx: Any,
        followup_findings: list[dict] | None = None,
        *,
        resume_session_id: str | None = None,
    ) -> dict:
        from app.services.runtime.bridge import RuntimeBridge, RuntimeCompletionMode
        from app.services.tooling.finalize_review import FinalizeReviewTool

        spec = build_review_perspective_spec(perspective)
        bridge = RuntimeBridge(
            llm_service=self._llm_service,
            tools=self._tools,
            user_id=self._user_id,
            session_factory=self._session_factory,
            agent_type=spec.agent_type,
        )
        if followup_findings:
            user_message = build_followup_prompt(followup_findings)
        else:
            user_message = "请开始按你的视角审查本次 PR diff，完成后用 FinalizeReview 提交结构化评论集。"
        sink = tag_event_sink(self._event_sink, perspective)
        if sink is not None:
            await sink({"type": "perspective_start"})

        if resume_session_id:
            # 09-P2: L3 会话续跑 — resume 时该视角已有会话锚点(session_start 早已上报,
            # stage 里存了 session_id), 不新建 bridge.run, 而是 continue 已有会话并提取 final_payload。
            # continue_session_until_payload: refresh 上下文 → run_once → _ensure_payload
            # (先扫快照取先前已落定的 finalizer payload, 无则继续逼出 FinalizeReview)。
            # 返回形状与 run 一致(final_payload/turn_count/tool_call_count), 下游共用。
            result = await bridge.continue_session_until_payload(
                session_id=resume_session_id,
                payload_extractor=bridge.extract_final_payload,
                finalizer_prompts=REVIEW_FINALIZER_PROMPTS,
                model_name=spec.agent_type,
                max_turns=self._max_turns,
                fallback_payload_builder=bridge._default_fallback_payload,
                finalizer_tools=[FinalizeReviewTool()],
                terminal_action_nudge_message=(
                    "审查尚未结构化终结：请调用 FinalizeReview 工具提交结构化评论集"
                    "（findings+summary），不要只用自然语言结束。"
                ),
            )
        else:
            # session 在 adapter.run 里创建, 创建瞬间即上报, 供头部显示每个视角的 sessionID。
            # 无 sink 时回调为空操作(保持"未配置 sink 的空转"既有行为)。
            async def on_session_created(session_id: str) -> None:
                if sink is not None:
                    await sink({"type": "session_start", "session_id": session_id})

            result = await bridge.run(
                project_id=self._project_id,
                task_id=self._task_id,
                system_prompt=spec.system_prompt,
                recon_payload=build_review_recon_payload(ctx),
                user_message=user_message,
                model_name=spec.agent_type,
                max_turns=self._max_turns,
                tool_allowlist=spec.tool_allowlist,
                event_sink=sink,
                finalizer_prompts=REVIEW_FINALIZER_PROMPTS,
                finalizer_tools=[FinalizeReviewTool()],
                terminal_action_nudge_message=(
                    "审查尚未结构化终结：请调用 FinalizeReview 工具提交结构化评论集"
                    "（findings+summary），不要只用自然语言结束。"
                ),
                on_session_created=on_session_created,
            )
        final_payload = result.get("final_payload") or {}
        # 12-P2.1: 兜底 payload(run 与 continue 两分支共用 _ensure_payload 产出)若自标
        # "未完成需重试"(_default_fallback_payload: findings=[] + requires_retry + INCOMPLETE),
        # 不得当作完成视角 —— 发 retry 事件 + raise, 由 orchestrator 记该视角失败,
        # review:* stage 不置 completed → 任务不会假完成(无最终结果)。
        if final_payload.get("requires_retry") or (
            final_payload.get("runtime_completion_mode") == RuntimeCompletionMode.INCOMPLETE.value
        ):
            if sink is not None:
                await sink({"type": "perspective_retry_needed", "perspective": perspective})
            raise PerspectiveResumeIncompleteError(
                f"perspective {perspective} incomplete (mode="
                f"{final_payload.get('runtime_completion_mode')})"
            )
        findings = [dict(item) for item in (final_payload.get("findings") or [])]
        if sink is not None:
            # 09-P1: perspective_done 带 findings 本体(非计数)+ session_id,
            # 供外层 sink 写 audit_stages 的 review:* stage 快照(resume 零 LLM 读取)。
            await sink(
                {
                    "type": "perspective_done",
                    "turn_count": result.get("turn_count"),
                    "session_id": result.get("session_id"),
                    "findings": findings,
                }
            )
        confidences = [float(f.get("confidence", 0.5)) for f in findings if isinstance(f, dict)]
        confidence = round(sum(confidences) / len(confidences), 3) if confidences else 0.8
        self._session_ids[perspective] = str(result.get("session_id") or "")
        return {
            "from_agent": perspective,
            "to_agent": "orchestrator",
            "summary": str(final_payload.get("summary") or ""),
            "key_findings": findings,
            "priority_areas": sorted({str(f.get("file_path")) for f in findings if f.get("file_path")}),
            "context_data": {
                "session_id": result.get("session_id"),
                "turn_count": result.get("turn_count"),
                "evidence_files": sorted({
                    str(f.get("file_path"))
                    for f in findings
                    if isinstance(f, dict) and f.get("file_path")
                }),
            },
            "confidence": confidence,
        }

    @property
    def session_ids(self) -> dict[str, str]:
        return dict(self._session_ids)
