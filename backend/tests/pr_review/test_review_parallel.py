"""spec §6 test_review_parallel: 三视角并行(gather), 结果合并去重正确。"""
import asyncio
import time

import pytest

from app.services.pr_review.orchestrator import ReviewOrchestrator
from app.services.pr_review.synthesizer import finding_to_comment


def _finding(source: str, severity: str = "high", path: str = "a.py", line: int = 3, category: str = "security") -> dict:
    return dict(
        rule_id=f"{source}-R1", severity=severity, category=category,
        title=f"{source} 发现", description="描述", file_path=path,
        line_start=line, line_end=line, confidence=0.8,
        needs_verification=False, verdict="confirmed", source=source,
    )


DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,5 @@
 import os
+import json
+x = 1
+y = 2
"""


def _make_dispatcher(delays: dict[str, float], handoff_builder, state: dict):
    async def dispatcher(perspective, ctx, followup_findings=None):
        state["inflight"] = state.get("inflight", 0) + 1
        state["max_inflight"] = max(state.get("max_inflight", 0), state["inflight"])
        try:
            await asyncio.sleep(delays.get(perspective, 0.01))
            return handoff_builder(perspective)
        finally:
            state["inflight"] -= 1

    return dispatcher


def test_three_perspectives_run_in_parallel():
    state: dict = {}
    delays = {"security": 0.12, "architecture": 0.12, "quality": 0.12}
    builder = lambda p: {"from_agent": p, "to_agent": "orchestrator", "summary": f"{p} ok",
                         "key_findings": [], "priority_areas": [], "context_data": {}, "confidence": 0.8}
    dispatcher = _make_dispatcher(delays, builder, state)
    ctx = type("C", (), {"diff_text": DIFF, "repo": "r", "pr_number": 1})()
    start = time.monotonic()
    review = asyncio.run(ReviewOrchestrator(dispatcher, enable_rules=False).run(ctx))
    elapsed = time.monotonic() - start
    assert state["max_inflight"] == 3, "三视角并发执行"
    assert elapsed < sum(delays.values()), "并行耗时应远小于串行累加"
    assert set(review.followup_rounds) == set()


def test_merged_dedup_same_file_line_category():
    """同 file+line+category 三视角重复 → 只留一条, 严重度取最高, 来源合并。"""
    handoffs = [
        {"from_agent": p, "to_agent": "orchestrator", "summary": p,
         "key_findings": [_finding(p, severity=sev)],
         "priority_areas": [], "context_data": {}, "confidence": 0.8}
        for p, sev in (("security", "high"), ("architecture", "critical"), ("quality", "medium"))
    ]

    async def dispatcher(perspective, ctx, followup_findings=None):
        return handoffs[["security", "architecture", "quality"].index(perspective)]

    ctx = type("C", (), {"diff_text": DIFF, "repo": "r", "pr_number": 1})()
    review = asyncio.run(ReviewOrchestrator(dispatcher, enable_rules=False).run(ctx))
    assert len(review.comments) == 1, "同 key 去重"
    merged = review.comments[0]
    assert merged.severity == "critical", "严重度取最高"
    assert "security" in merged.source and "architecture" in merged.source, "来源标注保留"
    benchmark = review.benchmark_comments[0]
    assert set(benchmark) >= {"path", "line", "body", "severity", "category"}


def test_cross_perspective_distinct_comments_kept():
    """不同 file/line/category 的评论互不去重。"""
    handoffs = [
        {"from_agent": p, "to_agent": "orchestrator", "summary": p,
         "key_findings": [_finding(p, path="a.py", line=idx + 2, category=cat)],
         "priority_areas": [], "context_data": {}, "confidence": 0.8}
        for idx, (p, cat) in enumerate((("security", "security"), ("architecture", "api"), ("quality", "bug")))
    ]

    async def dispatcher(perspective, ctx, followup_findings=None):
        return handoffs[["security", "architecture", "quality"].index(perspective)]

    ctx = type("C", (), {"diff_text": DIFF, "repo": "r", "pr_number": 1})()
    review = asyncio.run(ReviewOrchestrator(dispatcher, enable_rules=False).run(ctx))
    assert len(review.comments) == 3


def test_single_perspective_exception_keeps_others():
    """单视角抛异常不再炸掉整场(gather return_exceptions): 失败视角 0 findings 被标注,
    其余视角成果正常进综合层。"""
    details = {"architecture": (2, "bug"), "quality": (4, "api")}

    async def dispatcher(perspective, ctx, followup_findings=None):
        if perspective == "security":
            raise RuntimeError("LLM 流中断(paratera SSE 被掐)")
        line, category = details[perspective]
        return {"from_agent": perspective, "to_agent": "orchestrator", "summary": f"{perspective} ok",
                "key_findings": [_finding(perspective, severity="high", line=line, category=category)],
                "priority_areas": [], "context_data": {}, "confidence": 0.8}

    ctx = type("C", (), {"diff_text": DIFF, "repo": "r", "pr_number": 1})()
    review = asyncio.run(ReviewOrchestrator(dispatcher, enable_rules=False).run(ctx))
    assert len(review.comments) == 2, "architecture/quality 成果保留"
    assert "视角失败: security" in review.summary, "失败视角在摘要中自解释"


def test_empty_reason_when_all_filtered_by_severity():
    """全部候选 medium 且 min_severity=high → comments 空, 但 empty_reason 自解释
    而非看起来像 agents 没干活。"""
    lines = {"security": 2, "architecture": 3, "quality": 4}
    cats = {"security": "security", "architecture": "bug", "quality": "api"}

    async def dispatcher(perspective, ctx, followup_findings=None):
        return {"from_agent": perspective, "to_agent": "orchestrator", "summary": f"{perspective} ok",
                "key_findings": [_finding(perspective, severity="medium", line=lines[perspective], category=cats[perspective])],
                "priority_areas": [], "context_data": {}, "confidence": 0.8}

    ctx = type("C", (), {"diff_text": DIFF, "repo": "r", "pr_number": 1})()
    review = asyncio.run(ReviewOrchestrator(dispatcher, enable_rules=False, min_severity="high").run(ctx))
    assert review.comments == []
    assert review.empty_reason == "all_filtered_by_severity"
    assert review.synthesis.severity_dropped == 3
    assert "严重度过滤 3 条" in review.summary
