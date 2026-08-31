from __future__ import annotations

from pydantic import BaseModel

from app.services.review_runtime.models import ToolExecutionPayload
from app.services.runtime_core.tool_runtime import RuntimeTool, ToolExecutionContext, ToolRegistry, build_runtime_tool


class EchoInput(BaseModel):
    text: str


async def _echo_execute(parsed_input: EchoInput, context: ToolExecutionContext) -> ToolExecutionPayload:
    del context
    return ToolExecutionPayload(content=parsed_input.text, output_payload={"echo": parsed_input.text})


def test_build_runtime_tool_applies_restored_style_defaults():
    tool = build_runtime_tool(
        name="Echo",
        description="Echo text",
        input_model=EchoInput,
        execute=_echo_execute,
    )

    description = tool.describe()

    assert tool.name == "Echo"
    assert tool.user_facing_name({}) == "Echo"
    assert tool.aliases == []
    assert tool.search_hint is None
    assert tool.is_enabled() is True
    assert tool.is_concurrency_safe(EchoInput(text="alpha")) is False
    assert tool.is_read_only(EchoInput(text="alpha")) is False
    assert tool.is_destructive(EchoInput(text="alpha")) is False
    assert tool.interrupt_behavior() == "block"
    assert tool.requires_user_interaction() is False
    assert tool.should_defer is False
    assert tool.always_load is False
    assert description["read_only"] is False
    assert description["destructive"] is False
    assert description["interrupt_behavior"] == "block"
    assert description["requires_user_interaction"] is False
    assert description["should_defer"] is False
    assert description["always_load"] is False
    assert description["aliases"] == []
    assert description["search_hint"] is None


def test_build_runtime_tool_preserves_explicit_metadata_overrides():
    tool = build_runtime_tool(
        name="ReadFast",
        description="Fast read tool",
        input_model=EchoInput,
        execute=_echo_execute,
        aliases=["read_fast", "rf"],
        search_hint="fast file read",
        is_enabled=lambda: False,
        is_concurrency_safe=lambda parsed_input: True,
        is_read_only=lambda parsed_input: True,
        is_destructive=lambda parsed_input: False,
        interrupt_behavior=lambda: "cancel",
        requires_user_interaction=lambda: True,
        should_defer=True,
        always_load=True,
        user_facing_name=lambda raw_input: "Read Fast",
    )

    description = tool.describe()

    assert tool.user_facing_name({}) == "Read Fast"
    assert tool.aliases == ["read_fast", "rf"]
    assert tool.search_hint == "fast file read"
    assert tool.is_enabled() is False
    assert tool.is_concurrency_safe(EchoInput(text="alpha")) is True
    assert tool.is_read_only(EchoInput(text="alpha")) is True
    assert tool.interrupt_behavior() == "cancel"
    assert tool.requires_user_interaction() is True
    assert tool.should_defer is True
    assert tool.always_load is True
    assert description["aliases"] == ["read_fast", "rf"]
    assert description["search_hint"] == "fast file read"
    assert description["read_only"] is True
    assert description["interrupt_behavior"] == "cancel"
    assert description["requires_user_interaction"] is True
    assert description["should_defer"] is True
    assert description["always_load"] is True


def test_tool_registry_supports_alias_lookup_without_duplicate_descriptions():
    tool = build_runtime_tool(
        name="ReadFast",
        description="Fast read tool",
        input_model=EchoInput,
        execute=_echo_execute,
        aliases=["read_fast", "rf"],
    )
    registry = ToolRegistry([tool])

    described = registry.describe_tools()

    assert registry.get("ReadFast") is tool
    assert registry.get("read_fast") is tool
    assert registry.get("rf") is tool
    assert [item["name"] for item in described] == ["ReadFast"]


class DisabledEchoTool(RuntimeTool):
    name = "DisabledEcho"
    description = "Disabled echo"
    input_model = EchoInput
    aliases = ["disabled_echo"]
    search_hint = "disabled tool"

    def is_enabled(self) -> bool:
        return False

    async def execute(self, parsed_input: EchoInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        return ToolExecutionPayload(content=parsed_input.text, output_payload={"echo": parsed_input.text})


def test_tool_registry_omits_disabled_tools_from_descriptions_but_keeps_lookup():
    tool = DisabledEchoTool()
    registry = ToolRegistry([tool])

    assert registry.get("DisabledEcho") is tool
    assert registry.get("disabled_echo") is tool
    assert registry.describe_tools() == []



def test_tool_registry_hides_deferred_tools_until_activated():
    active_tool = build_runtime_tool(
        name="Read",
        description="Read files",
        input_model=EchoInput,
        execute=_echo_execute,
    )
    deferred_tool = build_runtime_tool(
        name="AskUser",
        description="Ask a human",
        input_model=EchoInput,
        execute=_echo_execute,
        should_defer=True,
        search_hint="ask the user a question",
    )
    tool_search = build_runtime_tool(
        name="ToolSearch",
        description="Search deferred tools",
        input_model=EchoInput,
        execute=_echo_execute,
        always_load=True,
        is_read_only=lambda parsed_input: True,
        is_concurrency_safe=lambda parsed_input: True,
    )
    registry = ToolRegistry([active_tool, deferred_tool, tool_search])

    assert [item["name"] for item in registry.describe_tools()] == ["Read", "ToolSearch"]
    assert [item["name"] for item in registry.describe_tools(active_tool_names=["AskUser"])] == ["Read", "AskUser", "ToolSearch"]
    assert [tool.name for tool in registry.deferred_tools()] == ["AskUser"]
