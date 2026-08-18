"""技能压缩恢复测试(阶段 14 S7):add/get/clear / agentId 隔离 / invoked_at
排序 / 预算截断(5K 单 / 25K 总)/ 压缩恢复端到端(skill_restore 注入
_recovery_reminder)。"""

import asyncio
from pathlib import Path

import pytest

from codesage.ai import ContentBlock, LLMResponse, StreamEvent
from codesage.core import user_message
from codesage.engine import AgentLoop, AgentLoopConfig, CompactionConfig
from codesage.permissions import PermissionEngine
from codesage.skills import (
    POST_COMPACT_MAX_TOKENS_PER_SKILL,
    POST_COMPACT_SKILLS_TOKEN_BUDGET,
    add_invoked_skill,
    build_restore_text,
    clear_invoked_skills,
    get_invoked_skills,
    reset_invoked_skills,
)
from codesage.tools import ToolRegistry


@pytest.fixture(autouse=True)
def _isolate_state():
    """每测清空进程内单例(隔离跨测试污染)。"""
    reset_invoked_skills()
    yield
    reset_invoked_skills()


# ---- add/get/clear ----

def test_add_get_clear():
    add_invoked_skill("review", "review prompt")
    add_invoked_skill("simplify", "simplify prompt")
    names = [s.name for s in get_invoked_skills()]
    assert set(names) == {"review", "simplify"}
    clear_invoked_skills()
    assert get_invoked_skills() == []


def test_agent_id_isolation():
    add_invoked_skill("review", "main prompt")
    add_invoked_skill("review", "sub prompt", agent_id="sub-1")
    assert [s.name for s in get_invoked_skills()] == ["review"]
    assert get_invoked_skills()[0].content == "main prompt"
    assert get_invoked_skills(agent_id="sub-1")[0].content == "sub prompt"
    clear_invoked_skills(agent_id="sub-1")
    assert get_invoked_skills(agent_id="sub-1") == []
    assert get_invoked_skills() != []  # 主会话不受影响


def test_invoked_at_ordering():
    add_invoked_skill("a", "first")
    add_invoked_skill("b", "second")  # 后调用 → 更近 → 排序在前
    names = [s.name for s in get_invoked_skills()]
    assert names == ["b", "a"]  # invoked_at 降序(最近优先)


def test_add_same_name_overwrites():
    add_invoked_skill("review", "v1")
    add_invoked_skill("review", "v2")
    (s,) = get_invoked_skills()
    assert s.content == "v2"


# ---- 预算截断(§10.2)----

def test_single_skill_truncated_to_5k_tokens():
    add_invoked_skill("big", "X" * (POST_COMPACT_MAX_TOKENS_PER_SKILL * 4 * 3))  # ~15K tokens
    text = build_restore_text()
    assert text is not None
    # 单技能 5K tokens × 4 chars/token = 20K 字符上限(+ 头部/标题开销)
    assert len(text) <= POST_COMPACT_MAX_TOKENS_PER_SKILL * 4 + 100


def test_total_budget_25k_tokens():
    # 6 个技能 × 8K tokens 各自 → 单技能截到 5K,总预算 25K → 只保留最近 5 个
    for i in range(6):
        add_invoked_skill(f"s{i}", "Y" * (POST_COMPACT_MAX_TOKENS_PER_SKILL * 4 * 2))
    text = build_restore_text()
    assert text is not None
    # 6 × 5K = 30K > 25K → 只收最近 5 个(第 6 个 s0 被预算挤出)
    assert "## s5" in text
    assert "## s0" not in text
    assert text.count("## s") == 5


def test_restore_none_without_skills():
    assert build_restore_text() is None


# ---- 压缩恢复端到端(skill_restore → _recovery_reminder)----

class FakeLLM:
    def __init__(self, script, summary_text="compacted"):
        self.script = script
        self.calls = 0
        self.summary_text = summary_text

    def stream(self, request, model="main"):
        return self._gen()

    async def _gen(self):
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for ev in self.script[idx](self.calls):
            await asyncio.sleep(0)
            yield ev

    async def complete(self, request, model="main"):
        return LLMResponse(content=[ContentBlock(type="text", text=self.summary_text)])


def text_event(text="answer"):
    return [StreamEvent(type="text_delta", text=text), StreamEvent(type="done", stop_reason="end_turn")]


def _big_history(n=6, size=400):
    return [user_message(f"hist-{i} " + "x" * size) for i in range(n)]


def _loop(llm, restore):
    return AgentLoop(
        AgentLoopConfig(
            client=llm,
            tools=ToolRegistry(),
            permissions=PermissionEngine(),
            compaction=CompactionConfig(window=100, reserve=10, keep_recent=200),
            cwd=Path("."),
            skill_restore=restore,
        )
    )


async def test_compact_injects_skill_restore():
    add_invoked_skill("review", "review the code carefully")
    loop = _loop(FakeLLM([lambda i: text_event("a")]), lambda: build_restore_text())
    result = await loop._compact(_big_history())
    assert result is not None
    assert loop._recovery_reminder is not None
    assert "Invoked skills (re-injected after compaction)" in loop._recovery_reminder
    assert "## review" in loop._recovery_reminder
    assert "review the code carefully" in loop._recovery_reminder


async def test_compact_skill_restore_none_leaves_reminder_unchanged():
    """skill_restore 回调返回 None → 无技能恢复段(零变化,不污染 reminder)。"""
    loop = _loop(FakeLLM([lambda i: text_event("a")]), lambda: None)
    result = await loop._compact(_big_history())
    assert result is not None
    assert "Invoked skills" not in (loop._recovery_reminder or "")


async def test_compact_skill_restore_merges_with_recovery_text():
    """技能恢复文本并入既有 recovery reminder(非覆盖,合并注入)。"""
    add_invoked_skill("greet", "say hi")
    loop = _loop(FakeLLM([lambda i: text_event("a")]), lambda: build_restore_text())
    await loop._compact(_big_history())
    # 无 modified files 时 recovery 文本为 None → restore 文本单独注入
    assert loop._recovery_reminder is not None
    assert "## greet" in loop._recovery_reminder
