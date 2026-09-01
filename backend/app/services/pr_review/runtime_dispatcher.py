"""运行时分发器(阶段 02 §3.2): 视角 → FindingRuntimeBridge(独立 session)。

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
    """视角运行规格(spec §3.5: build_review_runtime_spec 落点)。"""

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
        tools: dict[str, Any],
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

    async def __call__(self, perspective: str, ctx: Any, followup_findings: list[dict] | None = None) -> dict:
        from app.services.review_runtime.bridge import FindingRuntimeBridge
        from app.services.review_runtime.tools.finalize_review import FinalizeReviewTool

        spec = build_review_perspective_spec(perspective)
        bridge = FindingRuntimeBridge(
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
        findings = [dict(item) for item in (final_payload.get("findings") or [])]
        if sink is not None:
            await sink(
                {
                    "type": "perspective_done",
                    "turn_count": result.get("turn_count"),
                    "findings": len(findings),
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
