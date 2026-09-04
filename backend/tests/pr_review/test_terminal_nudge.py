"""spec §6 test_terminal_nudge: 自然结束未调 FinalizeReview → nudge ×2 → incomplete 标记。"""
import asyncio

from app.services.contracts.models import RuntimeCompletionMode, RuntimeTerminalAction
from tests.pr_review.fake_runtime import (
    ScriptedLLMService,
    ScriptedModelClient,
    build_review_runner,
    create_review_session,
    make_session_factory,
)

SAMPLE_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 import os
+import json
"""


def test_nudge_twice_then_incomplete(tmp_path):
    """模型只输出自然语言(从不调 FinalizeReview): 初始轮 + nudge×2 → incomplete。"""
    llm = ScriptedLLMService(
        turns=[
            {"content": "我看了一下 diff, 觉得没什么大问题。"},
            {"content": "(nudge 1) 我确认审查完成了。"},
            {"content": "(nudge 2) 审查完成。"},
        ]
    )
    factory = make_session_factory(tmp_path)
    store, runner, _ = build_review_runner(factory, ScriptedModelClient(llm), project_root=tmp_path)
    session_id = create_review_session(store, SAMPLE_DIFF, "你是 Security 视角。")
    result = asyncio.run(runner.run_once(session_id=session_id, model_name="review:security"))

    assert result.completion_mode is RuntimeCompletionMode.INCOMPLETE, "未结构化终结 → incomplete"
    assert result.terminal_action is RuntimeTerminalAction.NATURAL_END_WITHOUT_TERMINAL_ACTION
    assert llm.calls == 3, f"初始轮 + 2 次 nudge, 实际 {llm.calls} 次模型调用"


def test_terminal_call_within_nudge_budget_completes(tmp_path):
    """第 1 轮自然结束, nudge 后第 2 轮调用 FinalizeReview → 正常终结(预算内)。"""
    from tests.pr_review.fake_runtime import finalize_call

    llm = ScriptedLLMService(
        turns=[
            {"content": "先看 diff。"},
            {"tool_calls": [finalize_call([], "复核完成, 无可报告问题")]},
        ]
    )
    factory = make_session_factory(tmp_path)
    store, runner, _ = build_review_runner(factory, ScriptedModelClient(llm), project_root=tmp_path)
    session_id = create_review_session(store, SAMPLE_DIFF, "你是 Security 视角。")
    result = asyncio.run(runner.run_once(session_id=session_id, model_name="review:security"))

    assert result.completion_mode is not RuntimeCompletionMode.INCOMPLETE
    assert llm.calls == 2
