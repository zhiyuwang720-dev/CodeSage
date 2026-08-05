"""normalize_for_api tests: filtering, merging (toolResultsFirst), sentinel."""

from codesage.ai import ContentBlock
from codesage.core import NO_CONTENT_MESSAGE, assistant_message, normalize_for_api, user_message


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


def test_tool_result_merges_with_followup_text_tool_first():
    history = [
        assistant_message([ContentBlock(type="tool_use", id="t1", name="Bash", input={"cmd": "ls"})]),
        user_message([ContentBlock(type="tool_result", tool_use_id="t1", content="out")]),
        user_message("then what?"),
    ]
    out = normalize_for_api(history)
    # assistant(tool_use) -> single user message: tool_result first, then text
    assert [m.role for m in out] == ["assistant", "user"]
    assert [b.type for b in out[1].content] == ["tool_result", "text"]
    assert out[1].content[0].content == "out"
    assert out[1].content[1].text == "then what?"


def test_tool_result_merges_with_prior_text_tool_first():
    """Adjacent user messages merge; the merged content is toolResultsFirst."""
    out = normalize_for_api(
        [
            user_message("text"),
            user_message([ContentBlock(type="tool_result", tool_use_id="t", content="out")]),
        ]
    )
    assert len(out) == 1
    assert [b.type for b in out[0].content] == ["tool_result", "text"]
    assert out[0].content[1].text == "text"


def test_merge_reorders_multiple_tool_results_first():
    out = normalize_for_api(
        [
            user_message(
                [
                    ContentBlock(type="tool_result", tool_use_id="t1", content="a"),
                    ContentBlock(type="text", text="mid"),
                ]
            ),
            user_message(
                [
                    ContentBlock(type="text", text="tail"),
                    ContentBlock(type="tool_result", tool_use_id="t2", content="b"),
                ]
            ),
        ]
    )
    assert len(out) == 1
    assert [b.type for b in out[0].content] == ["tool_result", "tool_result", "text", "text"]
    assert [b.tool_use_id for b in out[0].content if b.type == "tool_result"] == ["t1", "t2"]


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


def test_mixed_blocks_stay_one_message_in_original_order():
    """A single user message keeps its block order; only merges reorder."""
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
    assert len(out) == 1
    assert [b.type for b in out[0].content] == ["text", "tool_result"]
    assert out[0].content[0].text == "see below"


# ---- empty content handling (Kode NO_CONTENT_MESSAGE) ----

def test_whitespace_only_text_block_dropped():
    out = normalize_for_api(
        [user_message([ContentBlock(type="text", text="  \n "), ContentBlock(type="text", text="real")])]
    )
    assert len(out) == 1
    assert [b.text for b in out[0].content] == ["real"]


def test_all_empty_blocks_become_sentinel():
    out = normalize_for_api([user_message([ContentBlock(type="text", text="   ")])])
    assert len(out) == 1
    assert out[0].content == NO_CONTENT_MESSAGE


def test_whitespace_string_becomes_sentinel_and_merges():
    out = normalize_for_api([user_message("   "), user_message("")])
    assert len(out) == 1
    assert out[0].content == f"{NO_CONTENT_MESSAGE}\n{NO_CONTENT_MESSAGE}"
