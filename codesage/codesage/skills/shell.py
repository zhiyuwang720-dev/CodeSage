"""技能内联 Shell 执行(阶段 14 §5.2):双模式正则 + 并行 + 逐条权限检查。

镜像 CC promptShellExecution:代码块 `` ```! \n cmd \n ``` `` 与行内
``!`cmd` `` 双模式,全部命令**并行**(asyncio.gather 同款 Promise.all),
每个命令执行前走完整权限检查 —— deny/ask(无授权)→ 整次技能调用失败
(CC MalformedCommandError 同款:非 allow 即抛);``allowed-tools: [Bash]``
的技能其 shell 块经 §7.1 授权自动放行;yolo 模式天然放行。

防替换注入:替换用从后往前切片拼接(函数式替换语义),杜绝 ``$&``/``$'``
特殊序列。输出上限复用 03 超大结果落盘阈值(超限落盘 + 预览)。
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from ..tools import ToolResult, ToolUseContext

if TYPE_CHECKING:
    from ..engine.loop import AgentLoop

#: 代码块模式:```! <cmd> ```(DOTALL 跨行)
_SHELL_FENCE_RE = re.compile(r"```!\n(.*?)```", re.DOTALL)
#: 行内模式:`` !`cmd` `` —— ``(?<=^|\s)`` 防误匹配 markdown inline code
#: (``!` `` 前必须是行首或空白)。Python 的 lookbehind 需定宽,``^`` 用
#: 零宽断言组合替代 CC 的 ``(?<=^|\s)``。
_SHELL_INLINE_RE = re.compile(r"(?:^|(?<=\s))!`([^`]*)`")
#: 廉价预检:不包含 ``!` `` 直接跳过正则(CC 同款)
_INLINE_MARKER = "!`"


class SkillPromptError(Exception):
    """技能提示词处理失败(解析 / shell 块权限拒绝),非 allow 即抛。"""


def _find_shell_blocks(text: str) -> list[tuple[re.Match, str]]:
    """发现全部 shell 块(代码块 + 行内),按出现位置排序。"""
    blocks = [(m, m.group(1).strip()) for m in _SHELL_FENCE_RE.finditer(text)]
    blocks += [(m, m.group(1).strip()) for m in _SHELL_INLINE_RE.finditer(text)]
    blocks.sort(key=lambda b: b[0].start())
    return blocks


def _shell_block_allowed(
    command: str, loop: "AgentLoop", bash_tool: Any, skill_allowed_tools: frozenset[str]
) -> bool:
    """逐条权限检查(§5.2):经 evaluate_tool_use 全链 + 审计。

    ``skill_allowed_tools`` 是技能自身授权(§7.1)—— 引擎第 8.5 步据此
    把「无规则无地板时的默认 ask」升级为 allow;deny/ask 规则、写保护、
    REQUIRES_EXPLICIT_APPROVAL 等硬地板全部在前,授权不豁免。
    """
    from ..permissions.store import load_permission_rules

    decision = loop.permissions.evaluate_tool_use(
        tool_name="Bash",
        tool_input={"command": command},
        tool=bash_tool,
        mode=loop.mode,
        cwd=loop.cwd,
        permissions=load_permission_rules(loop.settings) if loop.settings is not None else None,
        session_permissions=loop.session_permissions,
        skill_allowed_tools=frozenset(skill_allowed_tools),
    )
    return bool(decision.allowed)


def _truncate_output(content: str, index: int) -> str:
    """复用 03 超大结果落盘阈值:超限落盘 + 预览指针(R6)。"""
    from ..engine.tool_queue import _spill_large_result

    result = _spill_large_result(ToolResult(content=content), f"skill-shell-{index}")
    return str(result.content)


async def execute_shell_blocks(
    text: str,
    *,
    loop: "AgentLoop",
    skill_allowed_tools: frozenset[str] = frozenset(),
    timeout: int = 60,
) -> str:
    """执行正文中的全部内联 shell 块,返回替换后的文本。

    - 无 ``!` `` 标记 → 原样返回(廉价预检);
    - 权限拒绝(非 allow)→ 抛 :class:`SkillPromptError`,整次技能调用失败;
    - 命令经 Bash 工具既有 call 路径执行(继承 03 超时/kill/输出处理);
    - 输出超限落盘 + 预览(复用 tool_queue 溢出阈值)。
    """
    if _INLINE_MARKER not in text:
        return text
    blocks = _find_shell_blocks(text)
    if not blocks:
        return text
    bash_tool = loop.tools.get("Bash") if loop.tools else None
    if bash_tool is None:
        raise SkillPromptError("Bash tool unavailable for skill shell blocks")
    ctx = ToolUseContext(
        cwd=loop.cwd,
        abort_event=getattr(loop, "abort", None),
        command_source="agent_call",  # 与子代理一致:cd 限制在 cwd 内
        env=getattr(loop, "_tool_ctx", None).env if getattr(loop, "_tool_ctx", None) else None,
    )

    async def _run(command: str, index: int) -> str:
        if not _shell_block_allowed(command, loop, bash_tool, skill_allowed_tools):
            raise SkillPromptError(f"shell block denied: {command}")
        result: ToolResult | None = None
        async for yielded in bash_tool.call({"command": command, "timeout_ms": timeout * 1000}, ctx):
            result = yielded
        if result is None:
            raise SkillPromptError(f"shell block produced no result: {command}")
        content = result.content if isinstance(result.content, str) else str(result.content)
        return _truncate_output(content, index)

    # 并行执行(CC Promise.all 同款);任一失败(权限拒绝/工具错误)→ gather 传播
    outputs = await asyncio.gather(
        *[_run(cmd, i) for i, (_m, cmd) in enumerate(blocks)]
    )
    # 从后往前切片替换:位置不漂移,且无 re.sub 的 $&/$' 特殊序列注入面
    for (_match, _cmd), replacement in reversed(list(zip(blocks, outputs))):
        text = text[:_match.start()] + replacement + text[_match.end():]
    return text
