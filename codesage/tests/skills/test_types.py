"""技能定义模型测试(阶段 14 S1):字段白名单/默认值/frozen/连字符键映射。"""

import dataclasses

import pytest

from codesage.skills import FRONTMATTER_KEYS, SkillDefinition


def test_defaults():
    """未给字段全部走 spec 默认值。"""
    s = SkillDefinition(name="review", description="审查代码", body="审查提示词")
    assert s.name == "review"
    assert s.description == "审查代码"
    assert s.body == "审查提示词"
    assert s.when_to_use == ""
    assert s.argument_hint is None
    assert s.arguments == ()
    assert s.context == "inline"
    assert s.allowed_tools == frozenset()
    assert s.model is None
    assert s.effort is None
    assert s.agent is None
    assert s.shell is None
    assert s.paths == ()
    assert s.user_invocable is True
    assert s.disable_model_invocation is False
    assert s.hooks is None
    assert s.aliases == ()
    assert s.source == "project"
    assert s.skill_dir is None


def test_frozen_and_slots():
    """冻结 + slots:字段不可变、无 __dict__。"""
    s = SkillDefinition(name="a", description="d", body="b")
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.name = "x"
    assert not hasattr(s, "__dict__")


def test_full_construction():
    """全部字段显式构造。"""
    from pathlib import Path

    s = SkillDefinition(
        name="review",
        description="审查代码",
        body="审查 $ARGUMENTS",
        when_to_use="当用户要求审查代码质量时",
        argument_hint="[关注点]",
        arguments=("focus",),
        context="fork",
        allowed_tools=frozenset({"Read", "Grep", "Glob"}),
        model="sonnet",
        effort="quick",
        agent="Explore",
        shell="bash",
        paths=("src/**",),
        user_invocable=False,
        disable_model_invocation=True,
        hooks={"PreToolUse": "echo hi"},
        aliases=("r",),
        source="user",
        skill_dir=Path("/tmp/skills/review"),
    )
    assert s.context == "fork"
    assert s.allowed_tools == frozenset({"Read", "Grep", "Glob"})
    assert s.arguments == ("focus",)
    assert s.paths == ("src/**",)
    assert s.user_invocable is False
    assert s.disable_model_invocation is True
    assert s.hooks == {"PreToolUse": "echo hi"}
    assert s.source == "user"


def test_frontmatter_key_mapping():
    """连字符键 → snake_case 字段(CC 生态兼容,spec §3.2)。"""
    assert FRONTMATTER_KEYS["allowed-tools"] == "allowed_tools"
    assert FRONTMATTER_KEYS["argument-hint"] == "argument_hint"
    assert FRONTMATTER_KEYS["user-invocable"] == "user_invocable"
    assert FRONTMATTER_KEYS["disable-model-invocation"] == "disable_model_invocation"
    assert FRONTMATTER_KEYS["when_to_use"] == "when_to_use"  # 例外:下划线


def test_frontmatter_whitelist_covers_all_fields():
    """白名单覆盖全部构造字段(除派生字段 skill_dir/source)。"""
    fields = {
        "name", "description", "when_to_use", "argument_hint", "arguments",
        "context", "allowed_tools", "model", "effort", "agent", "shell",
        "paths", "user_invocable", "disable_model_invocation", "hooks", "aliases",
    }
    assert set(FRONTMATTER_KEYS.values()) == fields
