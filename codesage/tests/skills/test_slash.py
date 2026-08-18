"""REPL 斜杠技能兜底测试(阶段 14 S5 §6.1):技能命中 → run_single_turn 收到
解析提示词 / 内置命令优先 / unknown 保留 / user_invocable=False 拒绝 /
别名命中 / 授权累积到 loop._skill_allowed_tools。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from codesage.cli import repl as repl_mod
from codesage.cli.assemble import build_loop
from codesage.cli.repl import _handle_slash_command
from codesage.engine.loop import _render_reminder
from codesage.permissions import PermissionEngine
from codesage.skills import SkillDefinition, SkillRegistry
from codesage.tools import ToolRegistry


class _FakeLoop:
    """斜杠兜底所需的最小区块(真实 AgentLoop 的轻量子集)。"""

    def __init__(self):
        self.cwd = Path(".")
        self.session = None
        self._skill_allowed_tools: set[str] = set()
        self.permissions = PermissionEngine()
        self.tools = ToolRegistry()
        self.mode = "default"
        self.settings = None
        self.session_permissions = None
        self.abort = None
        self._tool_ctx = None

    def grant_skill_tools(self, names) -> None:
        self._skill_allowed_tools.update(names)


def _skill(name="greet", **kw) -> SkillDefinition:
    return SkillDefinition(name=name, description="d", body="Hello $ARGUMENTS", **kw)


def _state(skills, loop):
    return {
        "show_thinking": False,
        "transcript": False,
        "skills": skills,
        "loop": loop,
        "_bar_redraw": None,
    }


def _capture_run(monkeypatch):
    captured = {"prompt": None, "call_kw": None}

    async def fake_run_single_turn(loop, prompt, **kw):
        captured["prompt"] = prompt
        captured["call_kw"] = kw
        return SimpleNamespace(is_error=False, budget_exceeded=False, max_turns_exceeded=False)

    monkeypatch.setattr(repl_mod, "run_single_turn", fake_run_single_turn)
    return captured


async def test_slash_skill_hit_passes_resolved_prompt(monkeypatch, tmp_path):
    """技能命中 → run_single_turn 收到解析后的提示词(参数已替换)。"""
    skill_dir = tmp_path / "skills" / "greet"
    skill = SkillDefinition(name="greet", description="d", body="Hello $ARGUMENTS",
                            skill_dir=skill_dir)
    reg = SkillRegistry(builtin=[skill])
    loop = _FakeLoop()
    captured = _capture_run(monkeypatch)
    result = await _handle_slash_command(loop, "/greet world", _state(reg, loop))
    assert result is False  # 不退出 REPL
    assert captured["prompt"] == f"Base directory for this skill: {skill_dir}\n\nHello world"


async def test_slash_skill_grant_accumulates(monkeypatch):
    """inline 技能授权累积到 loop._skill_allowed_tools(§7.1 会话内累积)。"""
    reg = SkillRegistry(builtin=[_skill("review", allowed_tools=frozenset({"Read", "Grep"}))])
    loop = _FakeLoop()
    _capture_run(monkeypatch)
    await _handle_slash_command(loop, "/review src/a.py", _state(reg, loop))
    assert loop._skill_allowed_tools == {"Read", "Grep"}


async def test_slash_builtin_command_priority(monkeypatch):
    """内置命令恒优先于同名技能(CC builtInCommandNames 同款)。"""
    reg = SkillRegistry(builtin=[_skill("help")])
    loop = _FakeLoop()
    captured = _capture_run(monkeypatch)
    result = await _handle_slash_command(loop, "/help", _state(reg, loop))
    # 内置 /help 走 COMMANDS 处理器(返回 False 不退出),技能不触发
    assert captured["prompt"] is None
    assert result is False


async def test_slash_unknown_preserved(capsys):
    """全未命中 → 既有 unknown command 提示。"""
    reg = SkillRegistry(builtin=[_skill("greet")])
    loop = _FakeLoop()
    result = await _handle_slash_command(loop, "/nope", _state(reg, loop))
    assert result is False
    assert "unknown command: /nope" in capsys.readouterr().out


async def test_slash_no_skills_fallback_preserved(capsys):
    """未装配技能注册表 → unknown command 原样。"""
    loop = _FakeLoop()
    result = await _handle_slash_command(loop, "/nope", _state(None, loop))
    assert result is False
    assert "unknown command: /nope" in capsys.readouterr().out


async def test_slash_user_invocable_false_rejected(capsys):
    """user-invocable: false → 技能不可用户调用,拒绝。"""
    reg = SkillRegistry(builtin=[_skill("secret", user_invocable=False)])
    loop = _FakeLoop()
    result = await _handle_slash_command(loop, "/secret x", _state(reg, loop))
    assert result is False
    assert "cannot be invoked by the user" in capsys.readouterr().out


async def test_slash_alias_hit(monkeypatch):
    """技能别名同样参与兜底查找。"""
    reg = SkillRegistry(builtin=[_skill("greet", aliases=("g",))])
    loop = _FakeLoop()
    captured = _capture_run(monkeypatch)
    await _handle_slash_command(loop, "/g world", _state(reg, loop))
    assert captured["prompt"] == "Hello world"


# ---- 装配层:SkillTool + availableSkills 段注入(§9.1)----

def test_assemble_wires_skills(monkeypatch, tmp_path):
    """build_loop 装配:SkillTool 进工具池、availableSkills 进 bundle、loop 挂
    注册表;availableSkills 归 fixed 类,10 段上限内恒保留(08 机制)。"""
    monkeypatch.setenv("CODESAGE_CONFIG_DIR", str(tmp_path / "config"))
    loop = build_loop(cwd=tmp_path)
    assert "Skill" in [t.name for t in loop.tools.all()]
    assert any(t == "availableSkills" for t, _ in loop.context_bundle.sections)
    assert loop._skills is not None
    # _render_reminder 渲染出 # availableSkills 段(reminder 前缀机制,不持久化)
    reminder = _render_reminder(loop.context_bundle)
    text = reminder.content if isinstance(reminder.content, str) else str(reminder.content)
    assert "# availableSkills" in text
    assert "simplify:" in text  # bundled 演示技能在列表中
