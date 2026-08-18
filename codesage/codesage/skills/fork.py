"""fork 技能执行(阶段 14 §8):隔离子代理,复用 13 SubagentRunner。

``context: "fork"`` 的技能在隔离子代理中执行(CC executeForkedSkill 对齐):
三重隔离(§5.6 成文)由 SubagentRunner 天然提供 —— 独立消息历史/工具池/
模型指针;``allowed_tools`` 作为子代理 loop 初始授权(§7.1 同语义,授权而非
收窄)。结果回收:子代理终态文本 → tool_result,失败经 13 既有 is_error 传播
(保留清单 #2)。递归边界(§8 成文):子代理工具池含 Skill → fork 技能可在
子代理内再次触发,深度无硬限制(单层为主,max_turns 兜底)。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..tools import ToolResult
from .prompt import get_prompt_for_command
from .types import SkillDefinition

if TYPE_CHECKING:
    from ..engine.loop import AgentLoop
    from .registry import SkillRegistry


async def execute_forked_skill(
    skill: SkillDefinition,
    args: str,
    *,
    loop: "AgentLoop",
    registry: "SkillRegistry",
) -> ToolResult:
    """fork 技能执行:解析提示词 → SubagentRequest → SubagentRunner.run()。

    - ``agent`` 缺省 → general-purpose(CC command.agent ?? 'general-purpose');
    - ``model`` 缺省 → inherit(13 §8 链);
    - ``allowed_tools`` → 子代理初始授权(§7.1,授权而非收窄);
    - 后台化:技能 frontmatter 无后台语义(CC 同款,§8 成文),走既有
      run_in_background 工具参数。
    """
    from ..agents import AgentRegistry, SubagentRequest, SubagentRunner  # 函数级 import:破装配环

    prompt = await get_prompt_for_command(
        skill,
        args,
        session_id=loop.session.session_id if loop.session else "",
        cwd=loop.cwd,
        loop=loop,
    )
    agent_registry = AgentRegistry.from_default_paths(cwd=loop.cwd)
    req = SubagentRequest(
        prompt=prompt,
        name=skill.agent or "general-purpose",
        model=skill.model,
        allowed_tools=skill.allowed_tools or None,
        run_in_background=False,
    )
    runner = SubagentRunner(loop, req, agent_registry)
    result = await runner.run()
    return ToolResult(
        content=result.content,
        is_error=result.is_error,
        metadata={"skill": skill.name, "skill_output": True, **result.metadata},
    )
