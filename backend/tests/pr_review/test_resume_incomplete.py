"""spec 12 P2.1 回归: incomplete payload 不得当作完成视角。

- 视角级: RuntimePerspectiveDispatcher 对 _default_fallback_payload 兜底(requires_retry /
  runtime_completion_mode=incomplete)抛 PerspectiveResumeIncompleteError, 不发 perspective_done
  (否则 stage 被置 completed、orchestrator 综合空结果 → 任务"假完成", 即用户上报的问题 3);
- 编排级: 视角抛该异常 → orchestrator 的 return_exceptions 记该视角失败, 其 findings 不进
  综合层, summary 标注"视角失败"(任务不会拿空结果假完成)。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.pr_review.orchestrator import ReviewOrchestrator
from app.services.pr_review.runtime_dispatcher import PerspectiveResumeIncompleteError

# 新增行 2..3(供 findings 落在 enforce_lines 保留范围)
DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,4 @@
 import os
+import json
+x = 1
"""


def _finding(source: str, severity: str = "high", line: int = 3, category: str = "api") -> dict:
    return dict(
        rule_id=f"{source}-R1", severity=severity, category=category,
        title=f"{source} 发现", description="描述", file_path="a.py",
        line_start=line, line_end=line, confidence=0.8,
        needs_verification=False, verdict="confirmed", source=source,
    )


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        diff_text=DIFF, repo="o/r", pr_number=1, pr_key="o/r#1",
        related_files=[], git_history=[], ci_status=None, user_context=None,
    )


@pytest.mark.asyncio
async def test_dispatcher_rejects_incomplete_fallback_payload(monkeypatch):
    """RuntimePerspectiveDispatcher 对 requires_retry 兜底 payload 抛错, 不发 perspective_done。"""
    from app.services.pr_review.runtime_dispatcher import RuntimePerspectiveDispatcher

    class _FakeBridge:
        def __init__(self, **kwargs):
            pass

        async def run(self, **kwargs):
            return {
                "final_payload": {
                    "findings": [],
                    "requires_retry": True,
                    "runtime_completion_mode": "incomplete",
                    "is_final": False,
                },
                "session_id": "s-1",
                "turn_count": 0,
            }

    monkeypatch.setattr("app.services.runtime.bridge.RuntimeBridge", _FakeBridge)

    events: list[dict] = []

    async def sink(event: dict) -> None:
        events.append(event)

    dispatcher = RuntimePerspectiveDispatcher(
        llm_service=object(), tools=[], project_id="proj-1", event_sink=sink
    )

    with pytest.raises(PerspectiveResumeIncompleteError):
        await dispatcher("security", _ctx(), None)

    assert any(e.get("type") == "perspective_retry_needed" for e in events)
    assert not any(e.get("type") == "perspective_done" for e in events)


@pytest.mark.asyncio
async def test_orchestrator_marks_incomplete_perspective_failed():
    """视角抛 PerspectiveResumeIncompleteError → orchestrator 记失败, 其 findings 不进评论。"""

    async def dispatcher(perspective, ctx, followup_findings=None, *, resume_session_id=None):
        if perspective == "security":
            raise PerspectiveResumeIncompleteError("security incomplete")
        return {
            "from_agent": perspective, "to_agent": "orchestrator", "summary": f"{perspective} ok",
            "key_findings": [_finding(perspective)],
            "priority_areas": [], "context_data": {}, "confidence": 0.8,
        }

    orch = ReviewOrchestrator(dispatcher)
    review = await orch.run(_ctx(), resume_sessions={"security": "sess-s"})

    assert "视角失败" in review.summary
    assert "security" in review.summary
    # security 的 findings 未进综合层(失败视角 key_findings=[]), 其余视角照常
    # (architecture/quality 的相同 finding 会按 source 合并为一条, 故只断 >= 1)
    assert all(c.category != "security" for c in review.comments)
    assert len(review.comments) >= 1
