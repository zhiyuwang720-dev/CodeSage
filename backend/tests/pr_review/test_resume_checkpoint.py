"""spec 09 P5 验证门: resume 端到端 — 恢复快照(prefill) + 未完成视角续跑 → ReviewResult 合并。

走真实 command_router → orchestrator(不经执行器 fake pipeline), 验证:
- 已完成视角(prefill_handoffs)不进 gather, 恢复 findings 与未完成视角新产物同入综合层;
- 未完成视角带会话锚点 → resume_session_id 透传 dispatcher(L3 续跑);
- 合并去重后 ReviewResult.comments 同时含恢复与新增发现。
"""
from __future__ import annotations

from app.services.pr_review import command_router as cr

# 新增行 2..4(import json / x=1 / y=2), 供 findings 落在 enforce_lines 保留范围内
DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,5 @@
 import os
+import json
+x = 1
+y = 2
"""


def _finding(source: str, severity: str = "high", line: int = 2, category: str = "security") -> dict:
    return dict(
        rule_id=f"{source}-R1", severity=severity, category=category,
        title=f"{source} 发现", description="描述", file_path="a.py",
        line_start=line, line_end=line, confidence=0.8,
        needs_verification=False, verdict="confirmed", source=source,
    )


async def test_runtime_resume_merges_recovered_and_new_findings():
    """恢复快照(security/architecture) + 未完成视角(quality)续跑 → 3 条不同 key 全进评论。"""
    seen: dict[str, str | None] = {}
    findings_by_p = {
        "security": _finding("security", severity="critical", line=2, category="security"),
        "quality": _finding("quality", severity="high", line=3, category="api"),
    }

    async def dispatcher(perspective, ctx, followup_findings=None, *, resume_session_id=None):
        seen[perspective] = resume_session_id
        return {
            "from_agent": perspective, "to_agent": "orchestrator", "summary": f"{perspective} ok",
            "key_findings": [findings_by_p[perspective]],
            "priority_areas": [], "context_data": {}, "confidence": 0.8,
        }

    prefill = {
        "architecture": {
            "from_agent": "architecture", "to_agent": "orchestrator", "summary": "(resume: 检查点恢复)",
            "key_findings": [_finding("architecture", severity="high", line=4, category="bug")],
            "priority_areas": [], "context_data": {"resumed": True}, "confidence": 0.9,
        },
    }
    result = await cr.run_review_pipeline_async(
        diff_text=DIFF,
        options={
            "engine": "runtime",
            "dispatcher": dispatcher,
            "repo": "o/r", "pr_number": 1,
            "prefill_handoffs": prefill,
            "resume_sessions": {"quality": "sess-q"},
        },
    )

    assert len(result.comments) == 3, "security/architecture(恢复) + quality(新) 合并去重后全保留"
    assert seen["quality"] == "sess-q", "未完成视角透传会话锚点(L3 续跑)"
    assert seen["security"] is None, "未预填视角新建会话"
    assert "architecture" not in seen, "预填视角不进 gather(零 LLM)"


async def test_runtime_resume_empty_diff_still_terminates():
    """resume 遇空 diff(仅删除/文档): 预填被忽略, orchestrator 空评论集终结, 不炸。"""
    result = await cr.run_review_pipeline_async(
        diff_text="diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +0,0 @@\n-import os\n",
        options={
            "engine": "runtime",
            "dispatcher": object(),  # 空 diff 早退, 不会被调用
            "repo": "o/r", "pr_number": 1,
            "prefill_handoffs": {"security": {"key_findings": [{"file_path": "a.py"}]}},
            "resume_sessions": {"quality": "sess-q"},
        },
    )
    assert result.status == "completed"
    assert result.comments == []
