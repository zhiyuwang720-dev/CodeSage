"""normalize_for_api tests: filtering, merging (toolResultsFirst), sentinel,
reminder hoisting, compaction-summary boundary (phase 08)."""

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


# ---- phase 08: is_reminder hoisting / is_compaction_summary boundary ----

def test_reminder_hoisted_first():
    """A reminder buried mid-history lands at the front (byte-stable prefix)."""
    out = normalize_for_api(
        [
            user_message("question"),
            assistant_message("answer"),
            user_message("<system-reminder>context</system-reminder>", is_reminder=True),
        ]
    )
    assert [m.role for m in out] == ["user", "user", "assistant"]
    assert out[0].content == "<system-reminder>context</system-reminder>"
    assert out[1].content == "question"


def test_multiple_reminders_merge_in_order():
    out = normalize_for_api(
        [
            user_message("r1", is_reminder=True),
            user_message("r2", is_reminder=True),
            user_message("actual"),
        ]
    )
    assert len(out) == 2
    assert out[0].content == "r1\n\nr2"
    assert out[1].content == "actual"


def test_reminder_not_filtered_like_meta():
    """is_meta is dropped before API; is_reminder must be kept."""
    out = normalize_for_api(
        [user_message("drop me", is_meta=True), user_message("keep me", is_reminder=True)]
    )
    assert len(out) == 1
    assert out[0].content == "keep me"


def test_reminder_merges_with_other_reminders_only():
    """A reminder between normal messages joins the hoisted block, not neighbors."""
    out = normalize_for_api(
        [
            user_message("first"),
            user_message("reminder", is_reminder=True),
            user_message("second"),
        ]
    )
    assert [m.content for m in out] == ["reminder", "first\nsecond"]


def test_summary_never_merges_with_adjacent_users():
    out = normalize_for_api(
        [
            user_message("pre"),
            user_message("summarized history", is_compaction_summary=True),
            user_message("post"),
        ]
    )
    assert [m.content for m in out] == ["pre", "summarized history", "post"]


def test_summary_blocks_merge_of_neighbors_only_on_its_side():
    """Users on each side of a summary merge among themselves, not across it."""
    out = normalize_for_api(
        [
            user_message("pre1"),
            user_message("pre2"),
            user_message("summary", is_compaction_summary=True),
            user_message("post1"),
            user_message("post2"),
        ]
    )
    assert [m.content for m in out] == ["pre1\npre2", "summary", "post1\npost2"]
