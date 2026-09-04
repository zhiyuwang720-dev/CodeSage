from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.services.review_runtime.final_finding_contract import (
    FinalizedFindingPayload,
    format_validation_errors,
)
from app.services.contracts.models import ToolExecutionPayload
from app.services.runtime_core.tool_runtime import RuntimeTool, ToolExecutionContext


class InvalidFinalizeFindingInput:
    def __init__(self, raw_input: dict[str, Any], validation_error: ValidationError):
        self.raw_input = dict(raw_input or {})
        self.validation_error = validation_error


class FinalizeFindingTool(RuntimeTool):
    name = "FinalizeFinding"
    description = (
        "提交 Finding 阶段的最终结构化审计结论。这是终点工具，不是记录中间发现的工具。\n\n"
        "重要：一旦 FinalizeFinding 调用成功，Finding 阶段会立即终止，后续不会继续搜索、验证或补充漏洞。"
        "因此，只有在已经完成充分审计并准备结束整个 Finding 阶段时，才允许调用本工具。\n\n"
        "不要在发现第一个漏洞后立即调用本工具。发现一个漏洞后，应继续搜索其他独立攻击面，"
        "尝试发现更多 source→sink 利用链闭合的高危/严重漏洞。\n\n"
        "用法：\n"
        "- 已确认存在可报告漏洞时，提交 findings 数组，每个 finding 必须包含完整字段。\n"
        "- 审计完成且没有确认可报告漏洞时，可以提交 findings=[]，并在 summary 中说明审计范围、已检查证据和未确认漏洞的原因。\n"
        "- 如果 source、sink、exploit_chain、poc、impact、cve_justification 或 verification_notes 仍不完整，不要调用 FinalizeFinding。\n\n"
        "必须包含：\n"
        "- vulnerability_type、severity、title、description\n"
        "- file_path、line_start、line_end、code_snippet\n"
        "- source、sink、suggestion、confidence、needs_verification、verdict\n"
        "- exploit_chain、poc、impact、cve_justification、verification_notes\n\n"
        "审计要求：\n"
        "- 允许调用 FinalizeFinding 的条件：已经系统性检查主要高风险攻击面；findings 中每个漏洞都具备完整字段、"
        "闭合利用链、可复现 PoC、影响说明和 CVE 级别 justification；不存在明显还需要继续验证的高价值候选。\n"
        "- 如果 findings 数量较少，summary 必须说明已覆盖范围、为什么没有更多可报告漏洞、以及被排除的候选线索。\n"
        "- 禁止调用 FinalizeFinding 的情况：只找到第一个漏洞但尚未继续横向搜索；仍需读取文件、搜索引用、"
        "验证 source→sink、补齐 PoC/impact/cve_justification/verification_notes；还有 RCE、反序列化、SQL/NoSQL 注入、"
        "SSRF、权限绕过、文件操作等高风险方向未检查；只是想保存中间结果或阶段性总结。\n"
        "- 如果还需要继续读取文件、搜索引用、验证调用链、确认可利用性或补齐字段，必须继续调用 Read/Grep/Glob/Skill/PowerShell 等工具。\n"
        "- 如果审计已经完成，必须调用 FinalizeFinding 或输出可解析的 {\"findings\": [...], \"summary\": \"...\"} JSON。\n"
        "- 不要把最终漏洞细节放在 reason、notes 等自由文本字段中。\n"
        "- 不要只用自然语言宣布“审计完成”，必须提交结构化终点。"
    )
    input_model = FinalizedFindingPayload
    always_load = True

    def validate_input(self, raw_input: dict[str, Any]) -> FinalizedFindingPayload | InvalidFinalizeFindingInput:
        try:
            return FinalizedFindingPayload.model_validate(raw_input or {})
        except ValidationError as exc:
            return InvalidFinalizeFindingInput(raw_input or {}, exc)

    def is_concurrency_safe(self, parsed_input: Any = None) -> bool:
        del parsed_input
        return False

    async def execute(
        self,
        parsed_input: FinalizedFindingPayload | InvalidFinalizeFindingInput,
        context: ToolExecutionContext,
    ) -> ToolExecutionPayload:
        del context
        if isinstance(parsed_input, InvalidFinalizeFindingInput):
            validation_errors = format_validation_errors(parsed_input.validation_error)
            return ToolExecutionPayload(
                content=(
                    "FinalizeFinding 已拒绝本次提交，因为最终漏洞结论不是完整的结构化对象。"
                    "请继续调用工具补齐缺失字段，然后再次调用 FinalizeFinding。"
                ),
                output_payload={
                    "finalization_rejected": True,
                    "validation_errors": validation_errors,
                    "required_fields": [
                        "vulnerability_type",
                        "severity",
                        "title",
                        "description",
                        "file_path",
                        "line_start",
                        "line_end",
                        "code_snippet",
                        "source",
                        "sink",
                        "suggestion",
                        "confidence",
                        "needs_verification",
                        "verdict",
                        "exploit_chain",
                        "poc",
                        "impact",
                        "cve_justification",
                        "verification_notes",
                    ],
                },
                metadata={"finalization_rejected": True},
            )

        final_payload = parsed_input.model_dump(mode="json", exclude_none=True)
        return ToolExecutionPayload(
            content="Received final structured vulnerability findings.",
            output_payload={
                "final_payload": final_payload,
                "completion_mode": "finalize_tool",
                "terminal_action": "finalize_finding",
            },
            metadata={"finalize_finding": True},
        )
