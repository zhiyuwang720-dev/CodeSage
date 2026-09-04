"""spec §6 test_finalize_review: 缺必填字段 → finalization_rejected; 合法 payload → 成功终结。"""
import asyncio
import json

from app.services.contracts.models import RuntimeCompletionMode, RuntimeTerminalAction
from app.services.review_runtime.tools.finalize_review import FinalizeReviewTool
from tests.pr_review.fake_runtime import (
    ScriptedLLMService,
    ScriptedModelClient,
    build_review_runner,
    create_review_session,
    finalize_call,
    make_session_factory,
)

SAMPLE_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,4 @@
 import os
+import json
+password = 'hunter2000'
"""


def _finding(**overrides) -> dict:
    base = dict(
        rule_id="SEC-EVAL", severity="critical", category="security",
        title="动态执行", description="eval 注入风险", file_path="app.py",
        line_start=3, line_end=3, confidence=0.9,
        needs_verification=False, verdict="confirmed", source="security",
    )
    base.update(overrides)
    return base


def test_missing_required_fields_rejected():
    tool = FinalizeReviewTool()
    parsed = tool.validate_input({"findings": [{"rule_id": "X"}], "summary": "部分字段缺失"})
    payload = asyncio.run(tool.execute(parsed, context=None))
    assert payload.metadata.get("finalization_rejected") is True
    assert payload.output_payload["finalization_rejected"] is True
    assert payload.output_payload["validation_errors"], "错误反馈给模型"
    assert "severity" in json.dumps(payload.output_payload["validation_errors"])


def test_valid_payload_finalizes():
    tool = FinalizeReviewTool()
    parsed = tool.validate_input({"findings": [_finding()], "summary": "覆盖 app.py"})
    payload = asyncio.run(tool.execute(parsed, context=None))
    assert payload.output_payload["terminal_action"] == "finalize_review"
    assert payload.output_payload["final_payload"]["findings"][0]["rule_id"] == "SEC-EVAL"


def test_empty_findings_with_summary_allowed():
    tool = FinalizeReviewTool()
    parsed = tool.validate_input({"findings": [], "summary": "仅文档变更, 无可审内容"})
    payload = asyncio.run(tool.execute(parsed, context=None))
    assert payload.output_payload["terminal_action"] == "finalize_review"


def test_path_escape_rejected_by_contract():
    tool = FinalizeReviewTool()
    parsed = tool.validate_input({"findings": [_finding(file_path="/abs/evil.py")], "summary": "路径逃逸"})
    payload = asyncio.run(tool.execute(parsed, context=None))
    assert payload.output_payload["finalization_rejected"] is True


def test_real_queryloop_finalize_terminal(tmp_path):
    """真实 QueryLoop 链路: 视角注册表含 FinalizeReview; 调用后成功终结。"""
    llm = ScriptedLLMService(turns=[{"tool_calls": [finalize_call([_finding()], "审查完成, 1 条评论")]}])
    factory = make_session_factory(tmp_path)
    store, runner, registry = build_review_runner(factory, ScriptedModelClient(llm), project_root=tmp_path)
    tool_names = [t.name for t in registry.enabled_tools()]
    assert "FinalizeReview" in tool_names, "review:* 注册表自动挂 FinalizeReview"
    assert "FinalizeFinding" not in tool_names, "不挂 finding 终点工具"

    session_id = create_review_session(store, SAMPLE_DIFF, "你是 Security 视角。")
    result = asyncio.run(runner.run_once(session_id=session_id, model_name="review:security"))
    assert result.terminal_action is RuntimeTerminalAction.FINALIZE_REVIEW
    assert result.completion_mode is RuntimeCompletionMode.FINALIZE_TOOL
