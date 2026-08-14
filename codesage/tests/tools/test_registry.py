"""Tool registry tests."""

from codesage.tools import Tool, ToolRegistry, get_builtin_tools


def test_builtin_registration():
    tools = get_builtin_tools()
    names = [t.name for t in tools]
    assert names == [
        "LS", "Read", "Write", "Edit", "Glob", "Grep", "Bash",
        "TaskOutput", "TaskStop", "TodoWrite",
        "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "WebFetch",
        "Agent",
    ]


def test_register_lookup_and_override():
    registry = ToolRegistry(get_builtin_tools())
    assert registry.get("Read").name == "Read"
    assert registry.get("Nonexistent") is None

    class Fake(Tool):
        name = "Read"

    registry.register(Fake())
    assert type(registry.get("Read")) is Fake


def test_specs_generated_for_engine():
    registry = ToolRegistry(get_builtin_tools())
    specs = registry.specs()
    assert len(specs) == 16
    assert {s.name for s in specs} == {
        "LS", "Read", "Write", "Edit", "Glob", "Grep", "Bash",
        "TaskOutput", "TaskStop", "TodoWrite",
        "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "WebFetch",
        "Agent",
    }
    # every spec carries a schema with properties
    for spec in specs:
        assert "properties" in spec.input_schema
