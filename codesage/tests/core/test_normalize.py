"""normalize_for_api tests: filtering, merging, tool_result splitting."""

from codesage.ai import ContentBlock
from codesage.core import assistant_message, normalize_for_api, user_message


def test_drops_error_and_meta_messages():
    history = [
        user_message("hello"),
        assistant_message("answer", is_error=True),
        assistant_message("synthetic notice", is_meta=True),
        assistant_message("real answer"),
    ]
    out = normalize_for_api(history)
    assert [m.role for m in out] == ["user", "assistant"]
    assert out[1].content == "real answer"


def test_adjacent_user_messages_merge():
    out = normalize_for_api([user_message("first"), user_message("second")])
    assert len(out) == 1
    assert out[0].content == "first\nsecond"


def test_adjacent_assistant_messages_merge():
    out = normalize_for_api([assistant_message("a"), assistant_message("b")])
    assert len(out) == 1
    assert out[0].content == "a\nb"


def test_tool_result_split_from_text():
    history = [
        assistant_message([ContentBlock(type="tool_use", id="t1", name="Bash", input={"cmd": "ls"})]),
        user_message([ContentBlock(type="tool_result", tool_use_id="t1", content="out")]),
        user_message("then what?"),
    ]
    out = normalize_for_api(history)
    # assistant(tool_calls) -> user(tool_result) -> user(text)
    assert [m.role for m in out] == ["assistant", "user", "user"]
    assert out[1].content[0].type == "tool_result"
    assert out[2].content == "then what?"


def test_tool_result_never_merges_with_text():
    """A tool_result user message must stay separate from adjacent text users."""
    out = normalize_for_api(
        [
            user_message("text"),
            user_message([ContentBlock(type="tool_result", tool_use_id="t", content="out")]),
        ]
    )
    assert len(out) == 2  # text and tool_result remain distinct messages
    assert out[1].content[0].type == "tool_result"


def test_multiple_tool_results_in_one_message():
    out = normalize_for_api(
        [
            user_message(
                [
                    ContentBlock(type="tool_result", tool_use_id="t1", content="a"),
                    ContentBlock(type="tool_result", tool_use_id="t2", content="b"),
                ]
            )
        ]
    )
    assert len(out) == 1
    assert [b.tool_use_id for b in out[0].content] == ["t1", "t2"]


def test_mixed_blocks_split_text_and_tool():
    out = normalize_for_api(
        [
            user_message(
                [
                    ContentBlock(type="text", text="see below"),
                    ContentBlock(type="tool_result", tool_use_id="t1", content="x"),
                ]
            )
        ]
    )
    assert len(out) == 2
    assert out[0].content[0].text == "see below"
    assert out[1].content[0].type == "tool_result"
