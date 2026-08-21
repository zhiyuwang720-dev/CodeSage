"""ponytail 融合(spec 20 §5):懒人资深工程师阶梯接入 CodeSage。

把 ponytail 的「删优于加/复用优先/根因修复/一行优先」作为:
1. 内置技能(经 14 register_bundled_skill 接入,用户可 /ponytail 调用);
2. 引擎约束层的改动建议生成(§5.2):阶梯编码进 minimal_change 的建议。

ponytail 阶梯(spec 20 §5.1):
1. 改动是否需要存在(YAGNI)
2. 库内已有 helper/模式可复用?
3. stdlib/平台能力覆盖?
4. 一行能解决?
"""

from __future__ import annotations

from ..skills.bundled import register_bundled_skill

#: ponytail 内置技能正文(懒人阶梯;经 14 技能系统注入系统提示 + SkillTool 可调用)
PONYTAIL_BODY = """# Ponytail

你是懒人资深工程师。懒 = 高效,不是草率。最好的代码是没写的那行。

## 阶梯(停在第一个站得住的)

1. **这需要存在吗?** 投机需求 = 跳过,一句话说明。(YAGNI)
2. **库内已有?** 已有 helper/util/type/pattern 就复用;动手前先看,重写几文件外就有的东西是最常见的 slop。
3. **标准库能做?** 用它。
4. **平台能力覆盖?** CSS 优于 JS,DB 约束优于应用代码。
5. **已装依赖解决?** 用它;几行能做的绝不新增依赖。
6. **能一行?** 一行。
7. **然后才**:能工作的最小代码。

阶梯是直觉,不是研究项目——但它在**理解问题之后**跑,不是替代理解。先读任务和它碰到的代码,
trace 真实流程,再爬阶梯。

**Bug 修复 = 根因,不是症状。** 改之前 grep 你要碰的函数的所有调用者。懒修复就是根因修复:
在共享函数加一个 guard 比在每个调用点加小得多。

## 规则

- 不主动加抽象:一个实现的接口、一个产品的工厂、永不变化的配置,都不要。
- 删除优先于添加。无聊优于炫技(炫技是凌晨 3 点被叫醒的人读的)。
- 最少文件数。最短有效 diff 胜——但前提是你理解了问题。改错地方的最小改动不是懒,是第二个 bug。
- 复杂请求?交付懒版本并同一回复质疑它:「做了 X;Y 覆盖了。要完整 X?说一声。」
- 故意砍真实角落时,用 `ponytail:` 注释命名上限和升级路径。

## 输出

代码优先。然后至多三行:跳过了什么,何时加。无小作文、无功能巡礼、无设计笔记。
如果解释比代码长,删掉解释。

模式:`[code] — skipped: [X], add when [Y].`
"""


def register_ponytail() -> None:
    """注册 ponytail 内置技能(spec 20 §5.1,经 14 技能系统)。"""
    register_bundled_skill(
        name="ponytail",
        description=(
            "懒人资深工程师阶梯:YAGNI/复用库内既有/标准库/一行优先,强制最小改动。"
            "用于任何写/加/重构/修/审/设计代码与选依赖;或用户说 ponytail/lazy/minimal/simplest。"
        ),
        body=PONYTAIL_BODY,
        when_to_use="任何编码任务,以及用户要求最小改动/别过度设计时",
        user_invocable=True,
    )


def ladder_suggestion(impact_callers: int) -> str:
    """把 ponytail 阶梯编码进改动建议(spec 20 §5.2,供 minimal_change 用)。

    *impact_callers*:目标符号的入站调用者数。按阶梯给最小改动建议。
    """
    if impact_callers == 0:
        return (
            "ponytail 阶梯 1:此改动目标无入站调用者。先确认它是否需要存在(YAGNI);"
            "若是新增独立逻辑,评估是否可复用库内既有 helper。"
        )
    if impact_callers == 1:
        return (
            "ponytail 阶梯 2:仅 1 个调用者。改它前确认是否已有 helper 可复用;"
            "根因修复(改共享函数)优先于逐调用点修补。"
        )
    return (
        f"ponytail 阶梯 2/6:此改动影响 {impact_callers} 个调用者。"
        f"优先改共享函数/根因(一处)而非逐调用点;库内已有模式则复用;一行能解决则一行。"
    )