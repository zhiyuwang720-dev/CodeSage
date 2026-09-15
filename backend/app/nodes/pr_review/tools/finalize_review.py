"""FinalizeReview 终结工具(阶段 02 §3.4): PR 审查终点。

校验失败返回 finalization_rejected 反馈给模型, 校验通过返回 final_payload 并触发终止
(terminal_action)。
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.nodes.pr_review.contracts.final_review import (
    FinalReviewPayload,
    format_validation_errors,
)
from app.contracts.models import ToolExecutionPayload
from app.node_runtime.tool_gateway.runtime import RuntimeTool, ToolExecutionContext


class InvalidFinalizeReviewInput:
    def __init__(self, raw_input: dict[str, Any], validation_error: ValidationError):
        self.raw_input = dict(raw_input or {})
        self.validation_error = validation_error


class FinalizeReviewTool(RuntimeTool):
    name = "FinalizeReview"
    description = (
        "提交 PR 审查的最终结构化评论集。这是终点工具，不是记录中间发现的工具。\n\n"
        "重要：一旦 FinalizeReview 调用成功，审查阶段会立即终止，后续不会再读取代码或补充评论。"
        "因此，只有在已完成计划内的 diff 阅读与相关文件核对、准备结束整个审查阶段时，才允许调用本工具。\n\n"
        "用法：\n"
        "- 存在可报告问题时，提交 findings 数组，每条评论必须包含完整字段。\n"
        "- 审查完成且没有可报告问题时，提交 findings=[]，并在 summary 中说明审查范围、"
        "已检查的文件与未报告的原因。\n\n"
        "每条评论必须包含：\n"
        "- rule_id、severity(low/medium/high/critical)、category(bug/security/concurrency/data/api/perf/test_gap/doc_defect)\n"
        "- title、description、file_path、line_start、line_end、confidence、needs_verification、verdict、source\n\n"
        "硬性约束：\n"
        "- line_start/line_end 必须落在 diff 新增行(head 分支行号)；评论不新增行直接拒绝。\n"
        "- file_path 必须是仓库相对路径，禁止绝对路径或 ../ 逃逸。\n"
        "- source 必须填写当前视角(security/architecture/quality)。\n"
        "- Plan21 任务的每条 finding 必须填写本次工具返回的 evidence_refs；最终提交必须填写 "
        "assessment_scope（mode、snapshot_id、coverage_status、reviewed/unreviewed unit IDs）。\n"
        "- 不要把评论细节放在 summary 等自由文本字段；不要只用自然语言宣布“审查完成”。"
    )
    input_model = FinalReviewPayload
    always_load = True

    def __init__(self, review_context=None):
        self.review_context = review_context

    def validate_input(self, raw_input: dict[str, Any]) -> FinalReviewPayload | InvalidFinalizeReviewInput:
        try:
            return FinalReviewPayload.model_validate(raw_input or {})
        except ValidationError as exc:
            return InvalidFinalizeReviewInput(raw_input or {}, exc)

    def is_concurrency_safe(self, parsed_input: Any = None) -> bool:
        del parsed_input
        return False

    async def execute(
        self,
        parsed_input: FinalReviewPayload | InvalidFinalizeReviewInput,
        context: ToolExecutionContext,
    ) -> ToolExecutionPayload:
        del context
        if isinstance(parsed_input, InvalidFinalizeReviewInput):
            validation_errors = format_validation_errors(parsed_input.validation_error)
            return ToolExecutionPayload(
                content=(
                    "FinalizeReview 已拒绝本次提交，因为评论集不是完整的结构化对象。"
                    "请根据 validation_errors 补齐缺失字段后，再次调用 FinalizeReview。"
                ),
                output_payload={
                    "finalization_rejected": True,
                    "validation_errors": validation_errors,
                    "required_fields": [
                        "rule_id",
                        "severity",
                        "category",
                        "title",
                        "description",
                        "file_path",
                        "line_start",
                        "line_end",
                        "confidence",
                        "needs_verification",
                        "verdict",
                        "source",
                    ],
                },
                metadata={"finalization_rejected": True},
            )

        if self.review_context is not None:
            errors = self._validate_plan21(parsed_input)
            if errors:
                return ToolExecutionPayload(
                    content=(
                        "FinalizeReview 已拒绝本次提交：证据或覆盖声明与服务端 manifest 不一致。"
                    ),
                    output_payload={
                        "finalization_rejected": True,
                        "validation_errors": errors,
                    },
                    metadata={"finalization_rejected": True},
                )

        final_payload = parsed_input.model_dump(mode="json", exclude_none=True)
        return ToolExecutionPayload(
            content="Received final structured review comments.",
            output_payload={
                "final_payload": final_payload,
                "completion_mode": "finalize_tool",
                "terminal_action": "finalize_review",
            },
            metadata={"finalize_review": True},
        )

    def _validate_plan21(self, payload: FinalReviewPayload) -> list[dict[str, str]]:
        scope = payload.assessment_scope
        if scope is None:
            return [{"loc": "assessment_scope", "msg": "Plan21 review requires an assessment scope"}]
        errors: list[dict[str, str]] = []
        expected_mode = self.review_context.mode
        snapshot_id = (
            self.review_context.snapshot_reader.snapshot.snapshot_id
            if self.review_context.snapshot_reader is not None
            else None
        )
        if scope.mode != expected_mode:
            errors.append({"loc": "assessment_scope.mode", "msg": "mode does not match server capability"})
        if scope.snapshot_id != snapshot_id:
            errors.append({"loc": "assessment_scope.snapshot_id", "msg": "snapshot does not match server context"})
        required = {
            unit.unit_id
            for unit in self.review_context.diff_index.change_units
            if unit.status != "binary"
        }
        reviewed = set(scope.reviewed_unit_ids)
        unreviewed = set(scope.unreviewed_unit_ids)
        if reviewed - required or unreviewed != required - reviewed:
            errors.append({"loc": "assessment_scope", "msg": "coverage units do not reconcile with manifest"})
        if scope.coverage_status == "complete" and reviewed != required:
            errors.append({"loc": "assessment_scope.coverage_status", "msg": "complete coverage requires every text unit"})
        evidence = self.review_context.evidence_registry
        for index, finding in enumerate(payload.findings):
            references = list(finding.evidence_refs or [])
            known = [evidence[item] for item in references if item in evidence]
            if len(known) != len(references) or not known:
                errors.append({"loc": f"findings.{index}.evidence_refs", "msg": "finding requires known evidence from this run"})
            elif not any(item.get("kind") == "diff" for item in known):
                errors.append({"loc": f"findings.{index}.evidence_refs", "msg": "finding requires a diff evidence anchor"})
        return errors
