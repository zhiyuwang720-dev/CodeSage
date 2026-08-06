"""Base system prompt: the static cross-tool working rules (feat/08-core-prompt)."""

from codesage.cli.base_prompt import BASE_PROMPT, get_base_prompt


def test_base_prompt_keeps_core_identity_and_rules():
    assert "You are CodeSage" in BASE_PROMPT
    for rule in [
        "Read before you edit",
        "prefer Grep/Glob over assumptions",
        "Verify each tool result before relying on it",
        "switch strategy after two failures",
        "smallest change that works",
        "never invent file contents",
    ]:
        assert rule in BASE_PROMPT, f"missing rule: {rule}"


def test_base_prompt_is_static_and_formatted():
    """The prompt must stay a plain string (byte-stable cached prefix)."""
    assert isinstance(BASE_PROMPT, str)
    prompt = get_base_prompt("/tmp/x")
    assert "Working directory: /tmp/x" in prompt
    assert "{" not in prompt  # fully formatted, no dangling placeholders
    assert prompt.startswith("You are CodeSage")


def test_tool_descriptions_carry_usage_details():
    """Per-tool usage lives in the schema description, not the system prompt."""
    from codesage.tools import get_builtin_tools

    specs = {t.name: t.description for t in get_builtin_tools()}
    assert "Git Bash" in specs["Bash"] and "timeout_ms" in specs["Bash"]
    assert "must be Read first" in specs["Edit"]
    assert "offset/limit" in specs["Read"]
    assert "__pycache__" in specs["Glob"]
    assert "-A/-B/-C" in specs["Grep"]
