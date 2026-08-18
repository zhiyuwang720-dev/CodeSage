"""技能系统(阶段 14):定义、加载、注册表、提示词管道与双路径调用。

技能 = 将验证有效的 prompt 模板化的 Markdown 文件(SKILL.md:frontmatter +
正文提示词)。本包按 spec 14 交付:
- S1 定义层(SkillDefinition + frontmatter 白名单 + SAFE 白名单判定)
- S2 加载与注册表(发现/去重/优先级合并/列表预算/内置技能)
- S3 提示词管道(参数/环境变量/内联 shell 替换)
- S5 双路径调用(斜杠命令 + SkillTool)
- S7 压缩恢复(invoked_skills 注册表)
"""

from .bundled import bundled_skills, register_bundled_skill
from .loader import load_dir
from .registry import SkillRegistry
from .types import (
    FRONTMATTER_KEYS,
    SAFE_SKILL_PROPERTIES,
    SkillDefinition,
    skill_has_only_safe_properties,
)

__all__ = [
    "FRONTMATTER_KEYS",
    "SAFE_SKILL_PROPERTIES",
    "SkillDefinition",
    "SkillRegistry",
    "bundled_skills",
    "load_dir",
    "register_bundled_skill",
    "skill_has_only_safe_properties",
]
