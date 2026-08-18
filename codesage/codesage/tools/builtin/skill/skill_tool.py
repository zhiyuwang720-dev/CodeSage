"""SkillTool:模型路径的技能自动触发(阶段 14 §6.2)。

技能列表在系统提示的 Available skills 段;模型按名调用本工具。inline(默认)
返回解析后的提示词交模型**同轮**执行;fork(context='fork')派生子代理隔离
执行(§8,14 S6 落地,经 SubagentRunner 复用)。

契约声明(§6.2 成文):``needs_permissions`` 动态 —— 仅含安全属性的技能
(§7.3 SAFE 白名单)走既有 self-declared 路径自动 allow,否则默认 ask;
**不进 SYSTEM_TOOLS**(modes.py:25 注释「Skill is NOT whitelisted」已预留);
``is_concurrency_safe=False``(技能执行有副作用,顺序屏障)。
"""

from __future__ import annotations

from typing import Any

from ...base import Tool, ToolError, ToolResult, ToolUseContext
from ....skills import SkillRegistry
from ....skills.prompt import get_prompt_for_command

#: 工具描述内嵌用法指引(§9.3):模型无需额外注册表,从 Available skills 段取。
_SKILL_DESCRIPTION = (
    "在可用技能中查找并调用一个技能(技能列表见系统提示的 Available skills 段)。"
    "skill 名取列表中的名称(可带前导 /);args 是传给技能的参数文本,格式按该"
    "技能的 argument-hint。技能是专门化的提示词工作流(审查/重构/总结/专用"
    "任务),调用后按其指示执行。"
)


def _session_id(ctx: ToolUseContext) -> str:
    """当前会话 id(CODESAGE_SESSION_ID 替换用);无会话 → 空串。"""
    loop = getattr(ctx, "parent_loop", None)
    session = getattr(loop, "session", None) if loop is not None else None
    return getattr(session, "session_id", "") or ""


class SkillTool(Tool):
    name = "Skill"
    description = _SKILL_DESCRIPTION
    input_schema = {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "技能名(列表中的名称,可带前导 /)"},
            "args": {"type": "string", "description": "传给技能的参数文本"},
        },
        "required": ["skill"],
    }
    # 技能执行有副作用(授权累积、shell 块、fork 派生)—— 顺序屏障
    is_concurrency_safe = False

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    def needs_permissions(self, input: dict[str, Any]) -> bool:
        """§7.3 SAFE 白名单判定:仅安全属性 → False → 引擎 self-declared 路径
        allow(零引擎改动复刻 CC SAFE_SKILL_PROPERTIES);不安全/未知技能 → True。"""
        name = str(input.get("skill") or "").lstrip("/")
        try:
            skill = self._registry.get(name)
        except KeyError:
            return True  # 未知技能默认需确认
        return not self._registry.safe(skill)

    def validate_input(self, input: dict[str, Any]) -> None:
        """去前导 /;技能不存在 / 禁模型调用 → ToolError(引擎转 tool_result 自愈)。"""
        name = str(input.get("skill") or "").lstrip("/")
        if not name:
            raise ToolError("Skill: skill name required")
        try:
            skill = self._registry.get(name)
        except KeyError as exc:
            raise ToolError(f"Skill: {exc}") from None
        if skill.disable_model_invocation:
            raise ToolError(f"Skill: skill {skill.name!r} does not allow model invocation")
        input["skill"] = skill.name  # 归一化(别名 → 规范名)

    async def _run(self, input: dict[str, Any], ctx: ToolUseContext) -> ToolResult:
        name = str(input.get("skill") or "").lstrip("/")
        skill = self._registry.get(name)
        args = str(input.get("args") or "")
        if skill.context == "fork":
            # fork 技能:隔离子代理执行(§8)—— 14 S6 经 SubagentRunner 落地
            from ....skills.fork import execute_forked_skill  # 阶段 14 S6

            return await execute_forked_skill(
                skill, args, loop=ctx.parent_loop, registry=self._registry
            )
        prompt = await get_prompt_for_command(
            skill,
            args,
            session_id=_session_id(ctx),
            cwd=ctx.cwd,
            loop=ctx.parent_loop,
        )
        return ToolResult(
            content=prompt,
            metadata={
                "skill": skill.name,
                # §7.1 授权落点:引擎工具结果回收处读取并 grant(loop.py §6.3(3))
                "skill_allowed_tools": skill.allowed_tools,
                "skill_output": True,
            },
        )
