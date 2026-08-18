"""invoked_skills 进程内注册表(阶段 14 §10):压缩后技能恢复。

长会话一致性:技能 inline 执行后把**解析后的提示词**记入进程内注册表(按
agentId 隔离,键 = (agent_id or '', name)),压缩完成后经 skill_restore 回调
重注入恢复段 —— 技能指示不因上下文压缩而丢失(spec §10)。

- 触发点(§10.1,双路径同汇):斜杠命令 inline 执行前 + SkillTool inline 执行
  成功后;fork 技能不记录(fork 是隔离子代理,压缩发生在父上下文)。
- agentId 隔离(§2 裁决 7):fork 子代理完成时清理(13 runner 清理位)。
- 预算(§10.2):单技能 5K tokens / 总计 25K tokens,最近优先(超出截断)。
- 进程内存,不持久化;进程退出即释(R10)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..engine.tokens import estimate_tokens

#: 单技能恢复文本上限(POST_COMPACT_MAX_TOKENS_PER_SKILL)
POST_COMPACT_MAX_TOKENS_PER_SKILL = 5_000
#: 恢复段总预算(POST_COMPACT_SKILLS_TOKEN_BUDGET)
POST_COMPACT_SKILLS_TOKEN_BUDGET = 25_000


@dataclass(slots=True)
class InvokedSkill:
    """一次技能调用快照(压缩后重建用,content 已含替换)。"""

    name: str
    content: str  # 已含替换后的提示词
    invoked_at: float


#: 进程内单例:键 = (agent_id or '', name)
_add: dict[tuple[str, str], InvokedSkill] = {}


def add_invoked_skill(name: str, content: str, *, agent_id: str | None = None) -> None:
    """记录一次技能调用(同名重复调用覆盖前次,时间戳更新)。"""
    _add[(agent_id or "", name)] = InvokedSkill(name=name, content=content, invoked_at=time.time())


def get_invoked_skills(agent_id: str | None = None) -> list[InvokedSkill]:
    """按 invoked_at 降序(最近优先)返回该 agent 的技能调用记录。"""
    key = agent_id or ""
    items = [v for (aid, _n), v in _add.items() if aid == key]
    items.sort(key=lambda s: s.invoked_at, reverse=True)
    return items


def clear_invoked_skills(agent_id: str | None = None) -> None:
    """清除指定 agent(缺省 = 主会话)的全部记录。"""
    key = agent_id or ""
    for k in [k for k in _add if k[0] == key]:
        del _add[k]


def reset_invoked_skills() -> None:
    """清空整个注册表(测试/开发用;进程级单例隔离)。"""
    _add.clear()


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """按 token 预算截断(chars/4 启发式,与 estimate_tokens 同款)。"""
    if estimate_tokens(text) <= max_tokens:
        return text
    # 预算 * 4 字符 = 预算 tokens(非密集文本 chars/4);粗粒度够用,不切词
    return text[: max_tokens * 4]


def build_restore_text(agent_id: str | None = None) -> str | None:
    """压缩恢复段文本(§10.2):最近优先,单技能 5K / 总 25K 预算。"""
    skills = get_invoked_skills(agent_id=agent_id)
    if not skills:
        return None
    parts: list[str] = []
    used = 0
    for s in skills:
        text = truncate_to_tokens(s.content, POST_COMPACT_MAX_TOKENS_PER_SKILL)
        tokens = estimate_tokens(text)
        if used + tokens > POST_COMPACT_SKILLS_TOKEN_BUDGET:
            break
        used += tokens
        parts.append(f"## {s.name}\n{text}")
    return "Invoked skills (re-injected after compaction):\n\n" + "\n\n".join(parts)
