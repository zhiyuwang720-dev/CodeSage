"""spec §6 test_handoff_contract: 视角回传的 TaskHandoff 字段完整。"""
import asyncio
from dataclasses import fields as dc_fields

import pytest

from app.services.agent.agents.base import TaskHandoff
from app.services.pr_review.orchestrator import ReviewOrchestrator
from app.services.pr_review.runtime_dispatcher import (
    RuntimePerspectiveDispatcher,
    build_review_perspective_spec,
)

DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 import os
+import json
"""


def test_taskhandoff_dataclass_has_contract_fields():
    names = {f.name for f in dc_fields(TaskHandoff)}
    assert {"summary", "key_findings", "priority_areas", "context_data", "confidence"} <= names


def test_dispatcher_handoff_shape_contract():
    """RuntimePerspectiveDispatcher 的回传 dict 必须含协议全部字段。"""
    import inspect

    source = inspect.getsource(RuntimePerspectiveDispatcher.__call__)
    for field in ("from_agent", "to_agent", "summary", "key_findings", "priority_areas", "context_data", "confidence"):
        assert field in source, f"回传缺少 {field}"


def test_orchestrator_accepts_handoff_and_attributes_source():
    captured: dict = {}

    async def dispatcher(perspective, ctx, followup_findings=None):
        captured["perspectives"] = captured.get("perspectives", []) + [perspective]
        category = {"security": "security", "architecture": "api", "quality": "bug"}[perspective]
        return {
            "from_agent": perspective,
            "to_agent": "orchestrator",
            "summary": f"{perspective} 摘要",
            "key_findings": [
                dict(
                    rule_id=f"{perspective.upper()}-1", severity="high",
                    category=category,
                    title="t", description="d", file_path="a.py",
                    line_start=2, line_end=2, confidence=0.9,
                    needs_verification=False, verdict="confirmed", source=perspective,
                )
            ],
            "priority_areas": ["a.py"],
            "context_data": {"session_id": f"s-{perspective}"},
            "confidence": 0.8,
        }

    ctx = type("C", (), {"diff_text": DIFF, "repo": "r", "pr_number": 1})()
    review = asyncio.run(ReviewOrchestrator(dispatcher, enable_rules=False).run(ctx))
    assert captured["perspectives"] == ["security", "architecture", "quality"]
    assert len(review.comments) == 3
    for finding in review.comments:
        assert finding.source in {"security", "architecture", "quality"}, "每条评论带视角标记"


def test_perspective_spec_fields():
    spec = build_review_perspective_spec("security")
    assert spec.agent_type == "review:security"
    assert spec.system_prompt
    assert "Read" in spec.tool_allowlist
    with pytest.raises(ValueError):
        build_review_perspective_spec("nonexistent")
