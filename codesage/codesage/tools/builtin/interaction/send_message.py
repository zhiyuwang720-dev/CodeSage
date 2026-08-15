"""SendMessage: 队友通信原语(13 §6.3,11 §12 teammate 承诺)。

按 address_name 或 agent_id 寻址 → 目标 inbox(asyncio.Queue,随 runner 生命
周期)→ 目标 loop 每轮迭代前 drain 以 user 角色注入其 Message 流。目标不
存在/已终止 → 错误 tool_result(幂等,不阻塞,§6.3 失败语义)。

契约与 Agent 同族:needs_permissions()=True 且不进 SYSTEM_TOOLS —— 走完整
决策链 + 审计;is_concurrency_safe=True(投递零状态)。
"""

from __future__ import annotations

from typing import Any

from ...base import Tool, ToolError, ToolResult, ToolUseContext
from ....core.tasks import get_mailbox


class SendMessageTool(Tool):
    name = "SendMessage"
    description = (
        "Send a message to a running subagent (teammate), addressed by its "
        "address_name or agent_id. The message is injected into its context; "
        "delivery fails cleanly if the target has terminated."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "to": {"type": "string",
                   "description": "Target address_name or agent_id"},
            "message": {"type": "string", "description": "Message text"},
        },
        "required": ["to", "message"],
    }
    is_concurrency_safe = True  # §5.5 同款:投递零状态,并行成立
    user_facing_name = "SendMessage"

    def needs_permissions(self, input: dict[str, Any]) -> bool:
        return True  # 与 Agent 同契约:完整决策链 + 审计(§6.3)

    def validate_input(self, input: dict[str, Any]) -> None:
        to = input.get("to")
        if not isinstance(to, str) or not to.strip():
            raise ToolError("to is required (address_name or agent_id)")
        message = input.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ToolError("message is required")

    async def _run(self, input: dict[str, Any], ctx: ToolUseContext) -> ToolResult:
        ok, detail = get_mailbox().send(input["to"].strip(), input["message"].strip())
        if not ok:
            return ToolResult(detail, is_error=True)  # 幂等报错,不阻塞
        return ToolResult(f"message delivered to {input['to'].strip()}")