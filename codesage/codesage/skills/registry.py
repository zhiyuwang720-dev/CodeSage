"""技能注册表(阶段 14 S2):三层合并 + 别名 + 子集 + paths 过滤 + 列表预算。

优先级:**项目 > 用户 > 内置**(同名覆盖,Map 合并;镜像 13 §3.3)。注册表
只暴露 frontmatter 轻量字段,正文提示词懒执行(spec §2 裁决 2)。``safe()``
为 §7.3 SAFE 白名单判定(registry 持有,SkillTool 消费)。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from ..config import paths
from ..config.agents_md import find_git_root
from ..permissions.rules import path_rule_matches
from .bundled import bundled_skills
from .loader import load_dir
from .types import SkillDefinition, skill_has_only_safe_properties

#: listing 默认字符预算(1% × 200K × 4 chars/token 的简化常量,CC 对齐)
SKILLS_LISTING_BUDGET = 8_000
#: 环境变量覆盖预算(§9.2)
CODESAGE_SKILLS_LISTING_BUDGET = "CODESAGE_SKILLS_LISTING_BUDGET"
#: 单条描述硬上限(§9.2)
MAX_LISTING_DESC_CHARS = 250
#: 低于此 → names-only 极端模式(§9.2)
MIN_DESC_LENGTH = 20
#: 每条非 bundled 技能除描述外的固定开销估算(`- {name}: ` + ` - {when}`;CC 同款)
_FIXED_CHARS_PER_SKILL = 25


class SkillRegistry:
    """分层技能查找:project > user > builtin(spec §4.2)。"""

    def __init__(
        self,
        builtin: Iterable[SkillDefinition] = (),
        user_dir: Path | None = None,
        project_dir: Path | None = None,
        extra_dirs: Iterable[Path] = (),
    ) -> None:
        defs: dict[str, SkillDefinition] = {}
        for b in builtin:
            defs[b.name] = b
        if user_dir is not None:
            defs.update(load_dir(user_dir, source="user"))
        for extra in extra_dirs:
            defs.update(load_dir(extra, source="user"))
        if project_dir is not None:
            defs.update(load_dir(project_dir, source="project"))
        self._defs = defs
        self._rebuild_aliases()

    def _rebuild_aliases(self) -> None:
        """别名索引:alias → 规范技能名(构造/子集后重建)。"""
        self._aliases: dict[str, str] = {
            alias: s.name for s in self._defs.values() for alias in s.aliases
        }

    @classmethod
    def from_default_paths(cls, cwd: Path | None = None) -> "SkillRegistry":
        """用户级({config_dir}/skills)+ 项目级({git_root}/.codesage/skills)+ 内置。

        目录随 CodeSage 数据根(默认 ~/.codesage,可 CODESAGE_CONFIG_DIR 覆盖)
        与项目级配置前例(.codesage/settings.json),镜像 13 agents 约定。无
        git root → 回退 cwd 作为项目根(config/agents_md.py 同前例)。
        """
        start = (cwd or Path.cwd()).resolve()
        git_root = find_git_root(start)
        return cls(
            builtin=bundled_skills(),
            user_dir=paths.config_dir() / "skills",
            project_dir=(git_root or start) / ".codesage" / "skills",
        )

    def get(self, name: str, *, include_aliases: bool = True) -> SkillDefinition:
        """解析一个技能;KeyError 列出可用名单(CC parity,13 §4 同款)。"""
        if name in self._defs:
            return self._defs[name]
        if include_aliases and name in self._aliases:
            return self._defs[self._aliases[name]]
        available = ", ".join(sorted(self._defs)) or "(none)"
        raise KeyError(
            f"unknown skill {name!r}; available: {available}"
        ) from None

    def names(self) -> list[str]:
        """排序后的技能名(SkillTool 描述 / 调试用)。"""
        return sorted(self._defs)

    def all(self) -> list[SkillDefinition]:
        """全部技能定义(内置在前,保持优先级合并后的顺序)。"""
        return list(self._defs.values())

    def subset(self, names: Iterable[str]) -> "SkillRegistry":
        """子代理可见性收窄(§11.1):只保留给定名字(别名同样解析)+ 内置层。

        未知名静默跳过;内置技能恒保留(bundled 可发现性优先哲学同源,§4.4)。
        """
        selected: dict[str, SkillDefinition] = {}
        for name, skill in self._defs.items():
            if skill.source == "builtin":
                selected[name] = skill
        for n in names:
            try:
                skill = self.get(n)
            except KeyError:
                continue
            selected[skill.name] = skill
        return self._from_defs(selected)

    def safe(self, skill: SkillDefinition) -> bool:
        """§7.3 SAFE 白名单判定(纯安全属性 → 模型自动触发无需确认)。"""
        return skill_has_only_safe_properties(skill)

    # ---- listing ----

    def listing_text(self, *, budget: int | None = None, cwd: Path | None = None) -> str:
        """技能列表文本(注入 availableSkills 段,§9)。

        三阶段预算算法(CC formatCommandsWithinBudget 对齐,§9.2):
        1. 全量尝试:全部 ``- {name}: {desc} - {when}`` ≤ 预算 → 直接用;
        2. 分区处理:bundled 保留完整描述,非 bundled 均分剩余预算按
           ``max_desc_len`` 截断(超 250 先截 250);
        3. 极端模式:均分后每技能 < 20 字符 → 非 bundled 仅显示名称。

        无可见技能 → 空串(装配层据此跳过注入)。``paths`` 静态过滤按会话
        cwd 求值(§4.3),不匹配的技能不出现在列表。
        """
        if budget is None:
            budget = int(os.environ.get(CODESAGE_SKILLS_LISTING_BUDGET, SKILLS_LISTING_BUDGET))
        visible = [s for s in self._defs.values() if self._visible(s, cwd)]
        # 阶段 1:全量尝试
        full = [self._format_full(s) for s in visible]
        if sum(len(line) for line in full) <= budget:
            return "\n".join(full)
        bundled = [s for s in visible if s.source == "builtin"]
        unbundled = [s for s in visible if s.source != "builtin"]
        bundled_lines = [self._format_full(s) for s in bundled]
        bundled_len = sum(len(line) for line in bundled_lines)
        if not unbundled:
            # 全是内置:永不截断,直接给全量(bundled 可发现性优先,§4.4)
            return "\n".join(bundled_lines)
        # 阶段 2:非 bundled 均分剩余预算
        remaining = max(budget - bundled_len, 0)
        max_desc_len = max(0, min(remaining // len(unbundled) - _FIXED_CHARS_PER_SKILL, MAX_LISTING_DESC_CHARS))
        if max_desc_len < MIN_DESC_LENGTH:
            # 阶段 3:names-only 极端模式
            lines = bundled_lines + [f"- {s.name}" for s in unbundled]
        else:
            lines = bundled_lines + [self._format_trunc(s, max_desc_len) for s in unbundled]
        return "\n".join(lines)

    def _visible(self, skill: SkillDefinition, cwd: Path | None) -> bool:
        """paths 静态过滤(§4.3):gitignore 式模式对会话 cwd 求值。

        空 paths 全放行。模式按「相对仓库根」路径匹配(前导 ``/`` = 锚定根,
        与 gitignore 语义一致,Windows 路径无关);无斜杠的模式额外按 basename
        匹配任意深度(gitignore 同款)。复用 05 既有 path_rule_matches 语义。
        """
        if not skill.paths or cwd is None:
            return True
        cwd = cwd.resolve()
        root = find_git_root(cwd) or cwd
        try:
            rel: Path = cwd.relative_to(root)
        except ValueError:
            rel = cwd
        for pattern in skill.paths:
            p = pattern.replace("\\", "/").lstrip("/")
            if path_rule_matches(p, rel):
                return True
            if "/" not in p.rstrip("/"):
                # gitignore:无斜杠模式匹配任意深度下的 basename
                if rel.name and path_rule_matches(p, Path(rel.name)):
                    return True
        return False

    @staticmethod
    def _format_full(skill: SkillDefinition) -> str:
        if skill.when_to_use:
            return f"- {skill.name}: {skill.description} - {skill.when_to_use}"
        return f"- {skill.name}: {skill.description}"

    @staticmethod
    def _format_trunc(skill: SkillDefinition, max_desc_len: int) -> str:
        desc = skill.description
        if len(desc) > max_desc_len:
            desc = desc[:max_desc_len] + "…"
        when = f" - {skill.when_to_use}" if skill.when_to_use else ""
        return f"- {skill.name}: {desc}{when}"

    @classmethod
    def _from_defs(cls, defs: dict[str, SkillDefinition]) -> "SkillRegistry":
        """不经磁盘重载、直接以既有定义构造(子集用,镜像 13 内部构造)。"""
        self = cls.__new__(cls)
        self._defs = defs
        self._rebuild_aliases()
        return self
