"""spec §6 test_context_before_dispatch: 分发前完成上下文组装(ReviewContext 完整)。"""
import asyncio
import json

from app.services.pr_review.command_router import run_review_pipeline
from app.services.pr_review.orchestrator import ReviewOrchestrator
from app.services.pr_review.runtime_dispatcher import build_review_recon_payload

DIFF = """diff --git a/utils.py b/utils.py
--- a/utils.py
+++ b/utils.py
@@ -1,2 +1,4 @@
 def add(a, b):
     return a + b
+import json
+password = 'hunter-2024'
"""


def test_orchestrator_receives_fully_assembled_context():
    """分发器收到的 ctx 必须已是完整 ReviewContext(diff+历史+相关文件)。"""
    captured: dict = {}

    async def dispatcher(perspective, ctx, followup_findings=None):
        captured[perspective] = ctx
        return {"from_agent": perspective, "to_agent": "orchestrator", "summary": "",
                "key_findings": [], "priority_areas": [], "context_data": {}, "confidence": 0.8}

    ctx = type("C", (), {
        "diff_text": DIFF, "repo": "r", "pr_number": 1,
        "git_history": [type("H", (), {"sha": "abc", "author": "t", "message": "feat"})()],
        "related_files": [type("F", (), {"path": "caller.py", "reason": "caller", "content": "from utils import add"})()],
        "ci_status": None, "user_context": "注意 auth 模块",
    })()
    asyncio.run(ReviewOrchestrator(dispatcher, enable_rules=False).run(ctx))
    for perspective, received in captured.items():
        assert received is ctx, "分发的是同一(已组装)上下文对象"
        assert received.diff_text, "diff 已就绪"
        assert received.related_files and received.git_history, "上下文收集先于分发"


def test_recon_payload_captures_all_dimensions():
    ctx = type("C", (), {
        "diff_text": DIFF, "repo": "o/r", "pr_number": 7,
        "git_history": [type("H", (), {"sha": "abc", "author": "t", "message": "feat"})()],
        "related_files": [type("F", (), {"path": "caller.py", "reason": "caller", "content": "x"})()],
        "ci_status": {"check_runs": []}, "user_context": None, "pr_key": "o__r#7",
    })()
    payload = build_review_recon_payload(ctx)
    assert payload["repo"] == "o/r" and payload["pr_number"] == 7
    assert payload["diff_text"] == DIFF
    assert payload["related_files"][0]["path"] == "caller.py"
    assert payload["git_history"][0]["message"] == "feat"
    assert "ci_status" in payload
    json.dumps(payload, ensure_ascii=False)  # recon_payload 必须可 JSON 序列化


def test_rules_engine_pipeline_context_persisted(tmp_path, monkeypatch):
    """统一入口: 上下文产物(.auditai/context/<key>.json)先于审查结果落盘。"""
    monkeypatch.setenv("CODESAGE_PR_DATA_ROOT", str(tmp_path / "auditai"))
    result = run_review_pipeline(diff_text=DIFF, options={"repo": "demo", "pr_number": 1})
    context_file = tmp_path / "auditai" / "context" / "demo#1.json"
    assert context_file.is_file(), "ReviewContext 已落盘"
    assert result.context_path == "demo#1"
    assert result.review_id.startswith("demo#1-")
