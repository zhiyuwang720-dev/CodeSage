"""FinalizeReview 终结工具(阶段 02 §3.4): PR 审查终点。

校验失败返回 finalization_rejected 反馈给模型, 校验通过返回 final_payload 并触发终止
(terminal_action)。
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.services.contracts.final_review_contract import (
    FinalReviewPayload,
    format_validation_errors,
)
from app.services.contracts.models import ToolExecutionPayload
from app.services.tooling.runtime import RuntimeTool, ToolExecutionContext


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
        "- 不要把评论细节放在 summary 等自由文本字段；不要只用自然语言宣布“审查完成”。"
    )
    input_model = FinalReviewPayload
    always_load = True

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
