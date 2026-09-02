"""ReviewOrchestrator(阶段 02 §3.2): 星型分发 + 综合层 + 受控追问。

协作模型: 三视角调度平级、通信星型 —— 各自独立 session 内执行完整 ReAct 循环,
仅经 TaskHandoff 回传结构化结果; 视角间无直连通道; 综合层矛盾时可对单一视角
受控追问(同 session 续跑, ≤2 轮, 只传结构化事实)。

分发器可注入: 测试用 fake(确定性), 生产用 RuntimePerspectiveDispatcher(真 bridge)。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.services.agent.prompts.review_prompts import (
    REVIEW_ARCHITECTURE_PROMPT,
    REVIEW_QUALITY_PROMPT,
    REVIEW_SECURITY_PROMPT,
    build_followup_prompt,
)
from app.services.pr_review.rules import run_rules
from app.services.pr_review.synthesizer import (
    SynthesisResult,
    finding_to_comment,
    synthesize,
)
from app.services.review_runtime.final_review_contract import (
    SEVERITY_RANK,
    ReviewFinding,
)

PERSPECTIVES: tuple[str, ...] = ("security", "architecture", "quality")

PERSPECTIVE_PROMPTS: dict[str, str] = {
    "security": REVIEW_SECURITY_PROMPT,
    "architecture": REVIEW_ARCHITECTURE_PROMPT,
    "quality": REVIEW_QUALITY_PROMPT,
}

# 工具权限矩阵(§3.1): 键为 build_runtime_tool_registry 产出的工具名
# PowerShell: Windows 上 is_powershell_runtime_tool_enabled() 默认开启, 注册表已注册;
#           本机 bash 检测可能命中 WSL 启动器(System32\bash.exe), PowerShell 是可靠兜底 shell。
TOOL_MATRICES: dict[str, set[str]] = {
    # Security: Read/Glob/Grep + Bash/PowerShell(受限, 扫描反馈)
    "security": {"Read", "Glob", "Grep", "Bash", "PowerShell", "Skill"},
    # Architecture: Read/Glob/Grep + Bash/PowerShell(重读跨文件引用、验证构建/依赖)
    "architecture": {"Read", "Glob", "Grep", "Bash", "PowerShell", "Skill"},
    # Quality: Read/Glob/Grep + Bash/PowerShell(跑单测, 可选)
    "quality": {"Read", "Glob", "Grep", "Bash", "PowerShell", "Skill"},
}
# 追问轮次上限(§3.2.2: 每视角 ≤2)
MAX_FOLLOWUPS_PER_PERSPECTIVE = 2

DispatchFn = Callable[[str, Any, list[dict] | None], Awaitable[Any]]


@dataclass
class OrchestratedReview:
    """编排产物: 综合评论集 + 摘要 + 归因信息。"""

    comments: list[ReviewFinding] = field(default_factory=list)
    benchmark_comments: list[dict] = field(default_factory=list)
    summary: str = ""
    synthesis: SynthesisResult | None = None
    followup_rounds: dict[str, int] = field(default_factory=dict)
    rule_hits: int = 0
    empty_reason: str | None = None  # §7 无 diff 等情形

    def to_result_dict(self) -> dict:
        return {
            "summary": self.summary,
            "comments": [finding_to_comment(f) for f in self.comments],
            "followup_rounds": dict(self.followup_rounds),
            "rule_hits": self.rule_hits,
            "empty_reason": self.empty_reason,
        }


class ReviewOrchestrator:
    """固定流程: 上下文组装(阶段 01) → 规则层 → 三视角并行 → 综合层 → 受控追问 → 终结。"""

    def __init__(
        self,
        dispatcher: DispatchFn | None = None,
        *,
        perspectives: tuple[str, ...] = PERSPECTIVES,
        max_followups: int = MAX_FOLLOWUPS_PER_PERSPECTIVE,
        min_severity: str = "high",
        max_comments: int = 10,
        enable_rules: bool = True,
        enable_followups: bool = True,
    ):
        self._dispatcher = dispatcher
        self._perspectives = perspectives
        self._max_followups = max_followups
        self._min_severity = min_severity
        self._max_comments = max_comments
        self._enable_rules = enable_rules
        self._enable_followups = enable_followups

    async def run(self, ctx: Any) -> OrchestratedReview:
        # §7: 无 diff(仅删除/文档) → 无可审内容, 直接空评论集终结
        if not str(ctx.diff_text or "").strip():
            return OrchestratedReview(
                summary="无可审内容: diff 为空(仅删除或文档变更)",
                empty_reason="no_diff",
            )

        # 规则层先兜底(确定性, 模型不可用时可独立出审)
        rule_findings: list[ReviewFinding] = run_rules(ctx.diff_text) if self._enable_rules else []

        # 三视角并行分发(黑盒, asyncio.gather)。return_exceptions=True: 单视角硬异常
        # 记为该视角失败(0 findings + 备注), 不冒泡炸掉整个 run —— 其余视角成果照常进综合层。
        results = await asyncio.gather(
            *(self._dispatch(perspective, ctx, None) for perspective in self._perspectives),
            return_exceptions=True,
        )
        handoff_map: dict[str, dict] = {}
        for perspective, result in zip(self._perspectives, results):
            if isinstance(result, BaseException):
                error_note = f"{type(result).__name__}: {result}"
                handoff_map[perspective] = {
                    "from_agent": perspective,
                    "to_agent": "orchestrator",
                    "summary": f"(视角失败: {error_note})",
                    "key_findings": [],
                    "priority_areas": [],
                    "context_data": {"failed": True, "error": error_note},
                    "confidence": 0.0,
                }
            else:
                handoff_map[perspective] = result

        findings = [f.model_dump() for f in rule_findings]
        findings += [
            dict(item) for handoff in handoff_map.values() for item in (handoff.get("key_findings") or [])
        ]
        synthesis = synthesize(
            findings,
            diff_text=ctx.diff_text,
            max_comments=self._max_comments,
            min_severity=self._min_severity,
        )

        followup_rounds: dict[str, int] = {}

        # 受控追问: 触发条件 = 某视角的高严重度候选被综合层丢弃(落行/低噪),
        # 或视角在 context_data 自报 needs_followup(矛盾/证据不足)。
        if self._enable_followups and self._dispatcher is not None:
            for perspective in self._perspectives:
                handoff = handoff_map.get(perspective) or {}
                pending = self._high_severity_dropped(handoff, synthesis)
                if not pending and handoff.get("context_data", {}).get("needs_followup"):
                    pending = [dict(item) for item in (handoff.get("key_findings") or [])[:1]]
                rounds = 0
                while pending and rounds < self._max_followups:
                    followup_rounds[perspective] = rounds + 1
                    retry = await self._dispatch(perspective, ctx, pending)
                    rounds += 1
                    handoff_map[perspective] = retry
                    retry_findings = [dict(item) for item in (retry.get("key_findings") or [])]
                    synthesis = synthesize(
                        [f.model_dump() for f in rule_findings]
                        + self._findings_excluding(handoff_map, perspective)
                        + retry_findings,
                        diff_text=ctx.diff_text,
                        max_comments=self._max_comments,
                        min_severity=self._min_severity,
                    )
                    pending = self._high_severity_dropped(retry, synthesis)

        empty_reason = None
        if not synthesis.comments and synthesis.severity_dropped > 0:
            # 空评论不是因为没找到问题, 而是候选全部被 min_severity 过滤 —— 必须自解释,
            # 否则 CLI 静默输出 0 条, 看起来像 agents 没干活。
            empty_reason = "all_filtered_by_severity"

        return OrchestratedReview(
            comments=synthesis.comments,
            benchmark_comments=[finding_to_comment(f) for f in synthesis.comments],
            summary=self._summary(synthesis, handoff_map, followup_rounds),
            synthesis=synthesis,
            followup_rounds=followup_rounds,
            rule_hits=len(rule_findings),
            empty_reason=empty_reason,
        )

    async def _dispatch(self, perspective: str, ctx: Any, followup_findings: list[dict] | None) -> dict:
        if self._dispatcher is None:
            # 无分发器(纯规则模式): 返回空 handoff; 上下文完整性由本层保证
            return {
                "from_agent": perspective,
                "to_agent": "orchestrator",
                "summary": "(规则模式: 视角未启用)",
                "key_findings": [],
                "priority_areas": [],
                "context_data": {},
                "confidence": 0.0,
            }
        return await self._dispatcher(perspective, ctx, followup_findings)

    @staticmethod
    def _high_severity_dropped(handoff: dict, synthesis: SynthesisResult) -> list[dict]:
        """该视角提交的高严重度候选中被综合层剔除的部分(追问触发条件)。"""
        kept_keys = {f.dedup_key() for f in synthesis.comments}
        dropped: list[dict] = []
        for item in handoff.get("key_findings") or []:
            single = synthesize([dict(item)], enforce_lines=False)
            if not single.comments:
                continue
            finding = single.comments[0]
            if SEVERITY_RANK[finding.severity] >= SEVERITY_RANK["high"] and finding.dedup_key() not in kept_keys:
                dropped.append(dict(item))
        return dropped

    @staticmethod
    def _findings_excluding(handoff_map: dict[str, dict], perspective: str) -> list[dict]:
        """重综合时替换目标视角的候选, 其他视角保持不变(信息边界)。"""
        findings: list[dict] = []
        for source, handoff in handoff_map.items():
            if source == perspective:
                continue
            findings += [dict(item) for item in (handoff.get("key_findings") or [])]
        return findings

    @staticmethod
    def _summary(
        synthesis: SynthesisResult, handoff_map: dict[str, dict], followup_rounds: dict[str, int]
    ) -> str:
        parts = [f"综合评论 {len(synthesis.comments)} 条"]
        if synthesis.deduped_away:
            parts.append(f"去重剔除 {synthesis.deduped_away} 条")
        if synthesis.rejected_off_diff:
            parts.append(f"非新增行拒绝 {synthesis.rejected_off_diff} 条")
        if synthesis.severity_dropped:
            parts.append(f"严重度过滤 {synthesis.severity_dropped} 条")
        if followup_rounds:
            parts.append("追问: " + ", ".join(f"{p}×{n}" for p, n in followup_rounds.items()))
        failed = [p for p, h in handoff_map.items() if (h.get("context_data") or {}).get("failed")]
        if failed:
            parts.append("视角失败: " + ", ".join(failed))
        parts.append("视角: " + ", ".join(sorted(handoff_map)))
        return "; ".join(parts)
