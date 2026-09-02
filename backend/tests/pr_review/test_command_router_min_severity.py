"""min_severity 默认策略: runtime 引擎全量输出(low), rules 引擎低噪(high), 显式值覆盖。

背景: agents 在缺源码的 plain-diff 工作区里产出的多是 medium/low 置信度 findings,
runtime 默认 high 会把它们全滤掉 → 空评论像"没干活"(审计数据证实)。用户拍板 runtime 默认 low。
"""
from app.services.pr_review import command_router as cr
from app.services.pr_review.synthesizer import SynthesisResult

DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"


def _make_orchestrator_fake(captured: dict, review_result=None):
    class FakeReview:
        benchmark_comments = []

        def __init__(self) -> None:
            self.status = "completed"
            self.comments = []

        def to_result_dict(self) -> dict:
            return {"dummy": True}

    class FakeOrchestrator:
        def __init__(self, dispatcher, **kwargs) -> None:
            captured["min_severity"] = kwargs.get("min_severity")
            self.dispatcher = dispatcher

        async def run(self, ctx):
            return FakeReview()

    return FakeOrchestrator


async def test_runtime_default_min_severity_is_low(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(cr, "ReviewOrchestrator", _make_orchestrator_fake(captured))
    result = await cr.run_review_pipeline_async(
        diff_text=DIFF,
        options={"engine": "runtime", "dispatcher": object(), "repo": "o/r", "pr_number": 1},
    )
    assert captured["min_severity"] == "low", "runtime 引擎默认全量输出(low)"
    assert result.status == "completed"


async def test_runtime_explicit_min_severity_overrides(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(cr, "ReviewOrchestrator", _make_orchestrator_fake(captured))
    await cr.run_review_pipeline_async(
        diff_text=DIFF,
        options={"engine": "runtime", "dispatcher": object(), "repo": "o/r", "pr_number": 1, "min_severity": "critical"},
    )
    assert captured["min_severity"] == "critical", "显式 --min-severity 覆盖默认 low"


def test_rules_sync_keeps_high_default(monkeypatch):
    """rules 引擎(低噪原则)默认仍是 high, 不随 runtime 改 low。"""
    captured: dict = {}

    def fake_synthesize(findings, *, diff_text=None, max_comments=10, min_severity="high", enforce_lines=True):
        captured["min_severity"] = min_severity
        return SynthesisResult()

    monkeypatch.setattr(cr, "synthesize", fake_synthesize)
    cr.run_review_pipeline(
        diff_text=DIFF,
        options={"repo": "o/r", "pr_number": 1},
    )
    assert captured["min_severity"] == "high", "rules 默认保持高严重度低噪原则"
