"""spec §6 test_comment_on_added_lines: 评论 line 必须落在 diff 新增行, 违规被拒。"""
from app.services.pr_review.diff_lines import added_line_index, parse_added_lines
from app.services.pr_review.synthesizer import enforce_added_lines, synthesize
from app.services.review_runtime.final_review_contract import ReviewFinding

DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -10,7 +10,8 @@ def existing():
     keep_a()
+    added_one()
     keep_b()
+    added_two()
"""


def _finding(line: int) -> ReviewFinding:
    return ReviewFinding(
        rule_id="R", severity="high", category="bug", title="t", description="d",
        file_path="app.py", line_start=line, line_end=line, confidence=0.9,
        needs_verification=False, verdict="confirmed", source="security",
    )


def test_added_line_index_head_numbers():
    index = added_line_index(DIFF)
    assert index["app.py"] == {11, 13}, "head 分支行号: 11=added_one, 13=added_two"


def test_parse_added_lines_content():
    lines = parse_added_lines(DIFF)
    assert [(l.line, l.content.strip()) for l in lines] == [(11, "added_one()"), (13, "added_two()")]


def test_off_diff_line_rejected():
    valid, rejected = enforce_added_lines([_finding(11), _finding(12), _finding(20)], DIFF)
    assert [f.line_start for f in valid] == [11]
    assert rejected == 2


def test_deleted_file_comments_rejected():
    valid, rejected = enforce_added_lines([_finding(11)], "")
    assert valid == [] and rejected == 1


def test_synthesize_enforces_by_default():
    raw = [_finding(11).model_dump(), _finding(12).model_dump()]
    result = synthesize(raw, diff_text=DIFF)
    assert result.rejected_off_diff == 1
    assert [f.line_start for f in result.comments] == [11]
