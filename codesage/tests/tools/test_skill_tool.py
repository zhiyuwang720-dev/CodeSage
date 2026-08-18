"""SkillTool 测试(阶段 14 S5):inline 解析 / metadata 授权 / 引擎 grant 落点 /
validate_input 去前导 / 与错误 / needs_permissions SAFE 判定。"""

import asyncio
import json
from pathlib import Path

import pytest

from codesage.ai import ContentBlock, StreamEvent
from codesage.engine import AgentLoop, AgentLoopConfig
from codesage.permissions import PermissionEngine
from codesage.skills import SkillDefinition, SkillRegistry
from codesage.tools import ToolRegistry, ToolResult, ToolUseContext
from codesage.tools.builtin.skill import SkillTool


class FakeLLM:
    """脚本化事件序列的假 LLM(镜像 test_loop 同款)。"""

    def __init__(self, script):
        self.script = script
        self.calls = 0

    def stream(self, request, model="main"):
        return self._gen()

    async def _gen(self):
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for ev in self.script[idx](self.calls):
            await asyncio.sleep(0)
            yield ev


def tool_use_event(name, tid, input_json):
    return [
        StreamEvent(type="tool_use_start", tool_use_id=tid, tool_name=name),
        StreamEvent(type="tool_use_delta", input_json_delta=input_json),
        StreamEvent(type="done", stop_reason="tool_use"),
    ]


def text_event(text="answer"):
    return [StreamEvent(type="text_delta", text=text), StreamEvent(type="done", stop_reason="end_turn")]


def _loop(llm, tools, **kw):
    registry = ToolRegistry(tools)
    return AgentLoop(
        AgentLoopConfig(client=llm, tools=registry, permissions=PermissionEngine(), cwd=Path("."), **kw)
    )


def _ctx(parent_loop=None):
    return ToolUseContext(cwd=Path("."), parent_loop=parent_loop)


def _safe_skill(**kw) -> SkillDefinition:
    return SkillDefinition(name="s", description="d", body="do $ARGUMENTS", **kw)


# ---- validate_input ----

def test_validate_input_strips_leading_slash():
    tool = SkillTool(SkillRegistry(builtin=[_safe_skill()]))
    inp = {"skill": "/s", "args": "x"}
    tool.validate_input(inp)
    assert inp["skill"] == "s"  # 去前导 / 并归一化


def test_validate_input_unknown_skill_raises():
    tool = SkillTool(SkillRegistry(builtin=[_safe_skill()]))
    with pytest.raises(Exception) as e:
        tool.validate_input({"skill": "nope"})
    assert "unknown skill" in str(e.value)


def test_validate_input_empty_raises():
    tool = SkillTool(SkillRegistry(builtin=[_safe_skill()]))
    with pytest.raises(Exception) as e:
        tool.validate_input({"skill": ""})
    assert "skill name required" in str(e.value)


def test_validate_input_model_disabled_raises():
    tool = SkillTool(SkillRegistry(builtin=[_safe_skill(disable_model_invocation=True)]))
    with pytest.raises(Exception) as e:
        tool.validate_input({"skill": "s"})
    assert "does not allow model invocation" in str(e.value)


# ---- needs_permissions(§7.3 SAFE 判定)----

def test_needs_permissions_safe_false():
    """仅安全属性 → False → 引擎 self-declared 路径自动 allow。"""
    tool = SkillTool(SkillRegistry(builtin=[_safe_skill()]))
    assert tool.needs_permissions({"skill": "s"}) is False


def test_needs_permissions_unsafe_true():
    """allowed_tools 等不安全属性 → True → 走 ask。"""
    tool = SkillTool(SkillRegistry(builtin=[_safe_skill(allowed_tools=frozenset({"Read"}))]))
    assert tool.needs_permissions({"skill": "s"}) is True


def test_needs_permissions_unknown_true():
    tool = SkillTool(SkillRegistry(builtin=[_safe_skill()]))
    assert tool.needs_permissions({"skill": "ghost"}) is True


# ---- inline 执行 ----

async def test_inline_returns_prompt_and_metadata():
    skill = _safe_skill(allowed_tools=frozenset({"Read"}))
    tool = SkillTool(SkillRegistry(builtin=[skill]))
    ctx = _ctx(parent_loop=None)
    result = await tool._run({"skill": "s", "args": "x y"}, ctx)
    assert result.content == "do x y"  # 参数已替换
    assert result.metadata["skill"] == "s"
    assert result.metadata["skill_allowed_tools"] == frozenset({"Read"})
    assert result.metadata["skill_output"] is True


# ---- 引擎 grant 落点(loop.py §6.3(3))----

def _has_tool_result(messages) -> bool:
    for m in messages:
        if isinstance(m.content, str):
            continue
        if any(b.type == "tool_result" for b in m.content):
            return True
    return False


async def test_engine_grants_from_skill_metadata():
    """SkillTool 执行成功后,引擎工具结果回收处读取 metadata 并累积授权。"""
    skill = _safe_skill(allowed_tools=frozenset({"Read"}))
    tool = SkillTool(SkillRegistry(builtin=[skill]))
    llm = FakeLLM([
        lambda i: tool_use_event("Skill", "t1", json.dumps({"skill": "s", "args": "x"})),
        lambda i: text_event("done"),
    ])
    # 技能带 allowed_tools → 非 SAFE → 默认 ask;request_permission 批准后执行
    async def _approve(*a, **k):
        return True

    loop = _loop(llm, [tool], request_permission=_approve)
    collected = [m async for m in loop.run("hi")]
    assert _has_tool_result(collected)
    assert loop._skill_allowed_tools == {"Read"}  # 授权落点生效


async def test_engine_no_grant_without_metadata():
    """普通工具结果无 skill_allowed_tools → 授权集不变。"""
    tool = SkillTool(SkillRegistry(builtin=[_safe_skill()]))  # 空授权(SAFE 技能)
    llm = FakeLLM([
        lambda i: tool_use_event("Skill", "t1", json.dumps({"skill": "s"})),
        lambda i: text_event("done"),
    ])
    loop = _loop(llm, [tool])
    [m async for m in loop.run("hi")]
    assert loop._skill_allowed_tools == set()  # 空授权集不产生 grant
