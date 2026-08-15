"""Agent definition model tests (phase 13 S1): whitelist fields, defaults, frozen."""

from dataclasses import FrozenInstanceError

import pytest

from codesage.agents import AgentDefinition


def test_defaults():
    a = AgentDefinition(name="x", description="d", body="b")
    assert a.tools is None
    assert a.disallowed_tools == frozenset()
    assert a.model is None
    assert a.max_turns == 50
    assert a.permission_mode is None
    assert a.fork_context is False
    assert a.hooks is None
    assert a.background is False
    assert a.color is None
    assert a.source == "project"


def test_explicit_fields_roundtrip():
    a = AgentDefinition(
        name="n",
        description="d",
        body="b",
        tools=frozenset({"Read", "Bash"}),
        disallowed_tools=frozenset({"Agent"}),
        model="sonnet",
        max_turns=None,
        permission_mode="plan",
        fork_context=True,
        hooks={"PreToolUse": "echo hi"},
        background=True,
        color="green",
        source="user",
    )
    assert a.tools == frozenset({"Read", "Bash"})
    assert a.max_turns is None
    assert a.hooks == {"PreToolUse": "echo hi"}


def test_frozen_and_slots():
    a = AgentDefinition(name="x", description="d", body="b")
    with pytest.raises(FrozenInstanceError):
        a.name = "y"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        a.tools = frozenset({"Read"})  # type: ignore[misc]
    assert not hasattr(a, "__dict__")
