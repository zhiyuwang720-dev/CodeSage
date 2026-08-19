"""内建技能(阶段 14 §4.4):进程内单例 + register_bundled_skill 入口。

内置层优先级最高(listing 永不截断),内容以 Python 字符串内嵌(CC
SIMPLIFY_PROMPT 同款),无资源文件提取需求(spec 14 §1.2 裁剪表)。
演示技能 ``simplify``(只读,context=fork),用于 bundled 层机制测试、
「bundled 永不截断」断言与 fork 技能端到端。
"""

from __future__ import annotations
from .types import SkillDefinition

#: 进程内 bundled 技能单例(闭包私有;重复同名注册覆盖前者由注册表 Map 语义兜底)
_bundled_skills: list[SkillDefinition] = []


def register_bundled_skill(
    *,
    name: str,
    description: str,
    body: str,
    when_to_use: str = "",
    allowed_tools: frozenset[str] = frozenset(),
    user_invocable: bool = True,
    **kw,
) -> None:
    """注册一个内建技能到进程内单例(后续调用覆盖同名的先前注册)。"""
    _bundled_skills.append(
        SkillDefinition(
            name=name,
            description=description,
            body=body,
            when_to_use=when_to_use,
            allowed_tools=frozenset(allowed_tools),
            user_invocable=user_invocable,
            source="builtin",
            **kw,
        )
    )


def bundled_skills() -> list[SkillDefinition]:
    """当前进程已注册的全部内建技能(列表副本,调用方可变不污染单例)。"""
    return list(_bundled_skills)


def _clear_bundled_skills() -> None:
    """测试专用:清空单例(模块加载时注册的演示技能可被测试重置)。"""
    _bundled_skills.clear()


register_bundled_skill(
    name="simplify",
    description="简化代码:把传入的代码片段或文件改写为更简单、清晰的等价实现",
    when_to_use="当用户要求简化代码、去冗余、改善可读性时",
    argument_hint="[代码片段 | 文件路径]",
    context="fork",
    agent="general-purpose",
    allowed_tools=frozenset({"Read", "Grep", "Glob"}),
    body=(
        "You are a code simplification assistant. Given the code below, produce a\n"
        "simpler, clearer equivalent that preserves behavior. Only suggest changes;\n"
        "do NOT modify any files. Report the simplified version together with a brief\n"
        "note on what was simplified and why.\n\n"
        "$ARGUMENTS"
    ),
)
