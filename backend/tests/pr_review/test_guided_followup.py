"""spec §6 test_guided_followup: 矛盾时同 session 续跑一轮补充证据; >2 轮被拒。"""
import asyncio

from app.services.pr_review.orchestrator import MAX_FOLLOWUPS_PER_PERSPECTIVE, ReviewOrchestrator

DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,4 @@
 import os
+import json
+x = eval("1+1")
"""
# diff 新增行 = {2, 3}; 行 4 落行校验必拒


def _finding(source: str, note: str = "初版", line: int = 3) -> dict:
    return dict(
        rule_id="SEC-EVAL", severity="critical", category="security",
        title="动态执行", description=f"eval 注入风险({note})", file_path="a.py",
        line_start=line, line_end=line, confidence=0.8,
        needs_verification=False, verdict="confirmed", source=source,
    )


def test_followup_triggered_and_supplements_evidence():
    """视角自报 needs_followup → 编排器带结构化事实追问一轮 → 复核后收敛。"""
    calls: list[tuple[str, list[dict] | None]] = []

    async def dispatcher(perspective, ctx, followup_findings=None):
        calls.append((perspective, followup_findings))
        if perspective == "security" and followup_findings is None:
            return {
                "from_agent": perspective, "to_agent": "orchestrator",
                "summary": "发现 eval 但证据不足",
                "key_findings": [_finding("security", line=3)],
                "priority_areas": [], "context_data": {"needs_followup": True},
                "confidence": 0.4,
            }
        return {
            "from_agent": perspective, "to_agent": "orchestrator",
            "summary": f"{perspective} 完成",
            "key_findings": [_finding(perspective, note="复核后确认", line=3)] if perspective == "security" else [],
            "priority_areas": [], "context_data": {}, "confidence": 0.9,
        }

    ctx = type("C", (), {"diff_text": DIFF, "repo": "r", "pr_number": 1})()
    review = asyncio.run(ReviewOrchestrator(dispatcher, enable_rules=False).run(ctx))

    security_calls = [f for p, f in calls if p == "security"]
    assert len(security_calls) == 2, "同视角追问一轮(初始 + 1 次追问)"
    followup = security_calls[1]
    assert followup is not None and len(followup) == 1
    assert followup[0]["file_path"] == "a.py" and followup[0]["rule_id"] == "SEC-EVAL", "只传结构化事实"
    assert review.followup_rounds.get("security") == 1
    assert any(c.source == "security" for c in review.comments)


def test_followup_capped_at_two_rounds():
    """复核后候选仍被落行校验剔除 → 第 2 轮后强制停止(不允许无限追问)。"""
    calls: list[tuple[str, list[dict] | None]] = []

    async def dispatcher(perspective, ctx, followup_findings=None):
        calls.append((perspective, followup_findings))
        if perspective == "security":
            return {
                "from_agent": perspective, "to_agent": "orchestrator",
                "summary": "始终提交落在非新增行的评论",
                "key_findings": [_finding("security", line=4)],
                "priority_areas": [], "context_data": {"needs_followup": True},
                "confidence": 0.4,
            }
        return {
            "from_agent": perspective, "to_agent": "orchestrator", "summary": "ok",
            "key_findings": [], "priority_areas": [], "context_data": {}, "confidence": 0.8,
        }

    ctx = type("C", (), {"diff_text": DIFF, "repo": "r", "pr_number": 1})()
    review = asyncio.run(ReviewOrchestrator(dispatcher, enable_rules=False).run(ctx))

    security_calls = [1 for p, _ in calls if p == "security"]
    assert len(security_calls) == 1 + MAX_FOLLOWUPS_PER_PERSPECTIVE, "初始 + ≤2 轮追问"
    assert review.followup_rounds.get("security") == MAX_FOLLOWUPS_PER_PERSPECTIVE
    assert all(c.source != "security" for c in review.comments), "始终违规的候选最终被拒"
