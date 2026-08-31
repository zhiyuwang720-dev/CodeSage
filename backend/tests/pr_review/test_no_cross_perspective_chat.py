"""spec §6 test_no_cross_perspective_chat: 无视角间通信工具; 追问只传结构化事实。"""
import asyncio
import inspect

from app.services.agent.prompts.review_prompts import build_followup_prompt
from app.services.pr_review.orchestrator import TOOL_MATRICES, ReviewOrchestrator

DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 import os
+import json
"""

# 运行时已删除的平级通信类工具(冗余清理 §3.5)
FORBIDDEN_TOOLS = {"SendMessage", "send_message", "Broadcast", "AgentChat", "SendMessageTool", "Task"}


def test_permission_matrices_contain_no_cross_chat_tools():
    for perspective, allowlist in TOOL_MATRICES.items():
        hit = allowlist & FORBIDDEN_TOOLS
        assert not hit, f"{perspective} 权限矩阵含平级通信工具: {hit}"


def test_runtime_has_no_messaging_tool_registered(tmp_path):
    from tests.pr_review.fake_runtime import build_review_runner, make_session_factory

    factory = make_session_factory(tmp_path)
    _, _, registry = build_review_runner(factory, object().__new__(object), project_root=tmp_path)
    names = {t.name for t in registry.enabled_tools()}
    assert not (names & FORBIDDEN_TOOLS), f"运行时注册表含通信工具: {names & FORBIDDEN_TOOLS}"


def test_followup_prompt_only_structured_facts():
    """追问消息 = 评论 + 证据引用, 不含其他视角推理原文字段。"""
    findings = [
        dict(rule_id="SEC-EVAL", severity="critical", file_path="a.py",
             line_start=2, title="动态执行", code_snippet="eval(x)"),
    ]
    prompt = build_followup_prompt(findings)
    assert "a.py" in prompt and "SEC-EVAL" in prompt
    for banned in ("推理原文", "reasoning", "transcript", "architecture 视角认为", "quality 视角认为"):
        assert banned not in prompt, f"追问消息不应包含: {banned}"


def test_orchestrator_never_routes_reasoning_between_perspectives():
    """分发接口只接受 (perspective, ctx, 结构化事实); 无 '其他视角推理' 通道。"""
    signature = inspect.signature(ReviewOrchestrator._dispatch)
    params = list(signature.parameters)
    assert params == ["self", "perspective", "ctx", "followup_findings"]
    source = inspect.getsource(ReviewOrchestrator)
    assert "reasoning" not in source.lower().replace("reasoning_content", "")
