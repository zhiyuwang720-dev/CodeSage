"""技能定义模型(阶段 14 S1):冻结数据类 + 白名单字段。

字段镜像 CC PromptCommand 子集(参考 docs/reference/skill.md §5.4),frontmatter
键名 = CC 生态兼容 —— 多词键用连字符(allowed-tools / argument-hint / …),
when_to_use 例外用下划线(CC 同款,spec §3.2 成文)。未知键加载时忽略,
本模型只持有白名单字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

#: frontmatter 键 → 字段名映射(即白名单:只读这些键,其余忽略)
#: 键名与 CC 生态一致,字段名统一 snake_case
FRONTMATTER_KEYS: dict[str, str] = {
    "name": "name",
    "description": "description",
    "when_to_use": "when_to_use",  # 唯一用下划线的多词键(CC 同款)
    "argument-hint": "argument_hint",
    "arguments": "arguments",
    "context": "context",
    "allowed-tools": "allowed_tools",
    "model": "model",
    "effort": "effort",
    "agent": "agent",
    "shell": "shell",
    "paths": "paths",
    "user-invocable": "user_invocable",
    "disable-model-invocation": "disable_model_invocation",
    "hooks": "hooks",
    "aliases": "aliases",
}


@dataclass(slots=True, frozen=True)
class SkillDefinition:
    """技能定义:frontmatter + 正文提示词(「AI Shell 脚本」)。

    *body* 是 frontmatter 围栏之后的正文 = 技能提示词,只在调用时使用
    (发现与执行分离:技能列表只暴露 frontmatter 字段,spec 14 §2 裁决 2)。
    *skill_dir* 是 SKILL.md 所在目录,用于资源文件引用与
    ${CODESAGE_SKILL_DIR} 替换。
    """

    name: str  # frontmatter name;缺 name → 文件静默跳过
    description: str  # 技能描述(listing + 模型自动触发判断)
    body: str  # frontmatter 之后正文 = 提示词
    when_to_use: str = ""  # 自动触发条件描述(引导模型,非自动化触发器)
    argument_hint: str | None = None  # 参数提示(帮助/Tab 补全展示)
    arguments: tuple[str, ...] = ()  # 命名参数列表,映射到 $file 等
    context: Literal["inline", "fork"] = "inline"  # 执行隔离级别
    allowed_tools: frozenset[str] = frozenset()  # 工具授权(只豁免默认 ask,§7.1)
    model: str | None = None  # 仅存储,引擎消费留 19;fork 经 runner 消费
    effort: str | None = None  # 仅存储
    agent: str | None = None  # context='fork' 时使用的 Agent 定义名
    shell: str | None = None  # 仅存储;执行恒走 Bash 工具
    paths: tuple[str, ...] = ()  # gitignore 式;listing 静态过滤(§4.3)
    user_invocable: bool = True  # False → 用户不可 /name 调用
    disable_model_invocation: bool = False  # True → 模型不可 SkillTool 触发
    hooks: dict | None = None  # 仅解析存储,执行体 19
    aliases: tuple[str, ...] = ()
    source: str = "project"  # 'builtin' | 'user' | 'project'
    skill_dir: Path | None = None  # SKILL.md 所在目录(资源文件引用)


#: SAFE 白名单(§7.3):仅这些字段存在即视为安全属性;其余字段须为空值豁免。
#: 新增字段默认不在白名单 → 默认需确认(白名单前向兼容,§1.2 从严裁决)。
SAFE_SKILL_PROPERTIES: frozenset[str] = frozenset({
    "name", "description", "when_to_use", "argument_hint", "arguments",
    "aliases", "user_invocable", "disable_model_invocation", "source", "body",
})
#: 全部可判定字段(白名单 + 空值豁免的并集,§7.3)
_ALL_SKILL_FIELDS: frozenset[str] = frozenset(SkillDefinition.__dataclass_fields__)


def skill_has_only_safe_properties(skill: SkillDefinition) -> bool:
    """技能是否仅含安全属性(§7.3):白名单字段 + 空值豁免。

    不安全字段(allowed_tools / context(fork) / model / effort / agent /
    shell / paths / hooks / skill_dir)取默认空值时同样放行(CC 对
    undefined/null/空数组/空对象 跳过同款)。
    """
    for field in _ALL_SKILL_FIELDS:
        if field in SAFE_SKILL_PROPERTIES:
            continue
        if getattr(skill, field) in (None, (), frozenset(), False, "inline", ""):
            continue
        return False
    return True
