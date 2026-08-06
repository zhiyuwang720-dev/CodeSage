"""Compaction core tests (specs/08 §3.5): cut-point boundaries, serialization,
summary generation (mock LLM)."""

from pathlib import Path

import pytest

from codesage.ai import ContentBlock, LLMResponse
from codesage.core import assistant_message, user_message
from codesage.engine.compaction import (
    DEFAULT_RESERVE_TOKENS,
    MAX_RESULTS_BEFORE_CLEAN,
    OLD_RESULT_PLACEHOLDER,
    RECOVERY_MAX_CHARS_PER_FILE,
    SUMMARY_MAX_TOKENS_FRACTION,
    FileOps,
    _is_legal_cut,
    clean_old_tool_results,
    extract_file_ops,
    find_cut_point,
    generate_summary,
    recovery_reminder_text,
    serialize_conversation,
    summary_message,
)
from codesage.engine.tokens import estimate_message_tokens


def _u(text):
    return user_message(text)


def _a(text):
    return assistant_message(text)


def _tu(tid, name, input):
    return assistant_message([ContentBlock(type="tool_use", id=tid, name=name, input=input)])


def _tr(tid, content, error=False):
    return user_message(
        [ContentBlock(type="tool_result", tool_use_id=tid, content=content, is_error=error)]
    )


def _conversation():
    """Two complete turns plus one in flight (the last assistant reply)."""
    return [
        _u("build a calculator"),
        _tu("t1", "Bash", {"cmd": "ls"}),
        _tr("t1", "files: main.py test.py"),
        _a("done, calculator built"),
        _u("now add sqrt"),
        _tu("t2", "Edit", {"file_path": "calc.py", "old_string": "a", "new_string": "b"}),
        _tr("t2", "edited calc.py"),
        _a("sqrt added"),
    ]


def _tail_tokens(msgs, start):
    return sum(estimate_message_tokens(m) for m in msgs[start:])


# ---- find_cut_point ----

def test_cut_none_when_conversation_fits_window():
    assert find_cut_point(_conversation(), keep_recent=10**9) is None


def test_cut_none_on_empty_messages():
    assert find_cut_point([]) is None


def test_cut_none_when_single_message_fills_window():
    """A cut at index 0 would compress an empty span — refuse, no summary call."""
    msgs = [_u("x" * 100_000)]
    assert find_cut_point(msgs, keep_recent=1) is None


def test_cut_keeps_whole_turn_on_user_message():
    msgs = _conversation()
    # keep_recent == the last turn's tokens → accumulation reaches the budget
    # exactly at the user message that starts that turn: whole turn retained
    keep = _tail_tokens(msgs, 4)
    cut = find_cut_point(msgs, keep_recent=keep)
    assert cut is not None
    assert cut.index == 4
    assert cut.turn_prefix is None
    assert msgs[cut.index].role == "user"


def test_cut_splits_turn_on_assistant_message():
    msgs = _conversation()
    # keep_recent == exactly the final assistant reply → cut lands on it and
    # the turn's lead-in (user + tool round-trip) becomes the turn prefix
    keep = estimate_message_tokens(msgs[7])
    cut = find_cut_point(msgs, keep_recent=keep)
    assert cut is not None
    assert cut.index == 7
    assert cut.turn_prefix == msgs[4:7]


def test_cut_never_lands_on_tool_result():
    msgs = _conversation()
    # accumulation stops inside the tool_result round-trip (index 6); the
    # nearest legal cut is the assistant before it — never the carrier
    keep = estimate_message_tokens(msgs[6]) + estimate_message_tokens(msgs[7])
    cut = find_cut_point(msgs, keep_recent=keep)
    assert cut is not None
    assert cut.index < 6
    assert _is_legal_cut(msgs[cut.index])


def test_cut_prefix_starts_at_real_user_input():
    """The turn prefix never includes earlier tool_result carriers."""
    msgs = _conversation()
    keep = estimate_message_tokens(msgs[7])
    cut = find_cut_point(msgs, keep_recent=keep)
    assert cut.turn_prefix[0] is msgs[4]
    assert cut.turn_prefix[0].role == "user"
    assert isinstance(cut.turn_prefix[0].content, str)  # real input, not a tool_result


# ---- serialization ----

def test_serialize_truncates_long_tool_result():
    msgs = [_u("q"), _tu("t1", "Read", {"file_path": "x"}), _tr("t1", "x" * 3000)]
    text = serialize_conversation(msgs)
    assert "(truncated" in text
    assert "x" * 3000 not in text
    assert len(text) < 3000 + 500  # truncated + tags, not the full payload


def test_serialize_markup_blocks():
    msgs = [
        _u("user text"),
        assistant_message(
            [
                ContentBlock(type="thinking", text="internal"),
                ContentBlock(type="text", text="answer text"),
                ContentBlock(type="tool_use", id="t1", name="Bash", input={"cmd": "ls"}),
            ]
        ),
        _tr("t1", "output", error=True),
    ]
    text = serialize_conversation(msgs)
    assert "<user>user text</user>" in text
    assert "<thinking>internal</thinking>" in text
    assert "answer text" in text
    assert '<tool_call name="Bash" id="t1">' in text and '"cmd": "ls"' in text
    assert "<tool_result error>" in text and "output" in text


def test_serialize_wraps_conversation():
    text = serialize_conversation([_u("hi")])
    assert text.startswith("<conversation>\n") and text.endswith("\n</conversation>")


# ---- summary generation ----

class FakeCompleter:
    """Scripted non-streaming client for summary requests."""

    def __init__(self, texts=None, error=False):
        self.texts = list(texts or [])
        self.calls = []  # [(model, LLMRequest), ...]
        self.error = error
        self._i = 0

    async def complete(self, request, model="main"):
        self.calls.append((model, request))
        if self.error:
            return LLMResponse(is_error=True, error_message="boom")
        text = (
            self.texts[min(self._i, len(self.texts) - 1)]
            if self.texts
            else f"summary-{self._i}"
        )
        self._i += 1
        return LLMResponse(content=[ContentBlock(type="text", text=text)])


async def test_generate_summary_single_segment():
    msgs = _conversation()
    cut = find_cut_point(msgs, keep_recent=_tail_tokens(msgs, 4))
    client = FakeCompleter()
    summary = await generate_summary(client, msgs, cut=cut)
    assert summary == "summary-0"
    model, request = client.calls[0]
    assert model == "compact"
    assert request.max_tokens == int(DEFAULT_RESERVE_TOKENS * SUMMARY_MAX_TOKENS_FRACTION)
    assert "<conversation>" in request.messages[0].content
    assert "<existing-summary>" not in request.messages[0].content


async def test_generate_summary_uses_update_prompt_for_previous_summary():
    msgs = _conversation()
    msgs.insert(3, summary_message("earlier summary"))
    cut = find_cut_point(msgs, keep_recent=_tail_tokens(msgs, 5) + 1)
    assert cut is not None and cut.index > 3  # the summary is inside the compressed span
    client = FakeCompleter()
    await generate_summary(client, msgs, cut=cut)
    prompt = client.calls[0][1].messages[0].content
    assert "<existing-summary>" in prompt
    assert "earlier summary" in prompt


async def test_generate_summary_split_turn_requests_twice():
    msgs = _conversation()
    cut = find_cut_point(msgs, keep_recent=estimate_message_tokens(msgs[7]))
    assert cut is not None and cut.turn_prefix
    client = FakeCompleter(texts=["history", "prefix"])
    summary = await generate_summary(client, msgs, cut=cut)
    assert len(client.calls) == 2
    assert summary == "history\n\nprefix"


async def test_generate_summary_propagates_provider_error():
    msgs = _conversation()
    cut = find_cut_point(msgs, keep_recent=_tail_tokens(msgs, 4) + 1)
    client = FakeCompleter(error=True)
    with pytest.raises(Exception, match="boom"):
        await generate_summary(client, msgs, cut=cut)


async def test_summary_message_flag():
    msg = summary_message("s")
    assert msg.role == "user"
    assert msg.is_compaction_summary
    assert not msg.is_reminder


# ---- §3.6 fileOps restore ----

def _ops_conversation():
    return [
        _u("edit the file"),
        assistant_message(
            [
                ContentBlock(type="tool_use", id="e1", name="Edit", input={"file_path": "src/a.py"}),
                ContentBlock(type="tool_use", id="r1", name="Read", input={"file_path": "src/b.py"}),
            ]
        ),
        _tr("e1", "ok"),
        _tr("r1", "content"),
    ]


def test_extract_file_ops():
    ops = extract_file_ops(_ops_conversation())
    assert ops.read == ["src/b.py"]
    assert ops.modified == ["src/a.py"]


def test_extract_file_ops_skips_non_file_tools():
    msgs = [
        _u("q"),
        assistant_message(
            [ContentBlock(type="tool_use", id="t1", name="Bash", input={"command": "ls"})]
        ),
        _tr("t1", "out"),
    ]
    ops = extract_file_ops(msgs)
    assert ops.read == []
    assert ops.modified == []


def test_file_ops_merge_dedup_newest_first():
    old = FileOps(read=["a.py"], modified=["b.py", "c.py"])
    new = FileOps(read=["a.py", "d.py"], modified=["c.py"])
    merged = old.merged_with(new)
    assert merged.read == ["a.py", "d.py"]
    assert merged.modified == ["c.py", "b.py"]  # newest first, deduped


def test_file_ops_parse_append_roundtrip():
    ops = FileOps(read=["a.py"], modified=["b.py"])
    text = ops.append_to("summary body")
    assert text.startswith("summary body")
    assert "<modified-files>\nb.py\n</modified-files>" in text
    parsed = FileOps.parse(text)
    assert parsed.read == ["a.py"]
    assert parsed.modified == ["b.py"]
    assert FileOps.parse("no tags here").read == []


def test_file_ops_append_to_returns_text_unchanged_when_empty():
    assert FileOps().append_to("body") == "body"


def test_recovery_reminder_text(tmp_path):
    (tmp_path / "a.py").write_text("def f(): pass\n", encoding="utf-8")
    ops = FileOps(modified=["missing.py", "a.py"])  # missing skipped, a.py kept
    text = recovery_reminder_text(ops, tmp_path)
    assert text is not None
    assert "Recently modified files:" in text
    assert "# a.py" in text
    assert "def f(): pass" in text
    assert "missing.py" not in text


def test_recovery_reminder_text_none_without_modified(tmp_path):
    assert recovery_reminder_text(FileOps(read=["a.py"]), tmp_path) is None


def test_recovery_reminder_text_none_when_unreadable(tmp_path):
    ops = FileOps(modified=["nope.py"])
    assert recovery_reminder_text(ops, tmp_path) is None


def test_recovery_reminder_text_truncates_large_file(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("x" * (RECOVERY_MAX_CHARS_PER_FILE + 1000), encoding="utf-8")
    text = recovery_reminder_text(FileOps(modified=["big.py"]), tmp_path)
    assert text is not None
    assert "(truncated)" in text
    assert len(text) < RECOVERY_MAX_CHARS_PER_FILE + 2000


# ---- §3.7 old tool result cleanup ----

def _tool_round(name, tid, result):
    return [
        _u(f"do it {tid}"),
        assistant_message([ContentBlock(type="tool_use", id=tid, name=name, input={})]),
        _tr(tid, result),
    ]


def test_cleanup_triggers_on_count_and_keeps_newest():
    msgs = []
    for i in range(25):  # 25 tool rounds = 75 messages, 25 results
        msgs += _tool_round("Read", f"t{i}", f"result-{i}")
    cleaned, did = clean_old_tool_results(msgs, max_results=60, keep_recent=20)
    assert did
    assert len(cleaned) == len(msgs)  # structure preserved, only payloads swapped
    cleared = [
        block.content
        for msg in cleaned
        if isinstance(msg.content, list)
        for block in msg.content
        if block.type == "tool_result"
    ]
    assert cleared.count(OLD_RESULT_PLACEHOLDER) == 5  # 25 - 20 newest
    assert cleared[-20:] == [f"result-{i}" for i in range(5, 25)]  # newest intact


def test_cleanup_does_not_fire_under_threshold():
    msgs = []
    for i in range(15):
        msgs += _tool_round("Read", f"t{i}", f"result-{i}")
    cleaned, did = clean_old_tool_results(msgs, max_results=60, keep_recent=20)
    assert not did
    assert cleaned is msgs


def test_cleanup_whitelist_only():
    msgs = []
    for i in range(25):
        msgs += _tool_round("Edit", f"t{i}", f"result-{i}")  # not in whitelist
    cleaned, did = clean_old_tool_results(msgs, max_results=60, keep_recent=20)
    assert not did  # nothing clearable → no change


def test_cleanup_fires_on_stale_interval():
    """Time path fires below the count threshold — old results go stale."""
    msgs = []
    for i in range(3):
        msgs += _tool_round("Bash", f"t{i}", f"result-{i}")
    cleaned, did = clean_old_tool_results(
        msgs, max_results=60, keep_recent=2, now=2_000.0, last_clean=0.0
    )
    assert did
    assert cleaned[2].content[0].content == OLD_RESULT_PLACEHOLDER  # oldest cleared
    assert cleaned[8].content[0].content == "result-2"  # newest two intact


def test_cleanup_unknown_tool_kept():
    msgs = []
    for i in range(25):
        msgs += _tool_round("WeirdTool", f"t{i}", f"result-{i}")
    cleaned, did = clean_old_tool_results(msgs, max_results=60, keep_recent=20)
    assert not did


def test_extract_file_ops_newest_first(tmp_path):
    """Recovery must re-inject the MOST RECENT edits (specs/08 §3.6)."""
    msgs = []
    for i in range(8):
        (tmp_path / f"f{i}.py").write_text(f"content {i}", encoding="utf-8")
        msgs += [
            _u(f"edit {i}"),
            assistant_message(
                [ContentBlock(type="tool_use", id=f"e{i}", name="Edit", input={"file_path": f"f{i}.py"})]
            ),
            _tr(f"e{i}", "ok"),
        ]
    ops = extract_file_ops(msgs)
    assert ops.modified == [f"f{i}.py" for i in range(7, -1, -1)]  # newest first
    text = recovery_reminder_text(ops, tmp_path)
    assert text is not None
    assert "# f7.py" in text and "# f3.py" in text  # the last 5
    assert "f2.py" not in text  # f2..f0 dropped


def test_extract_file_ops_skips_denied_and_failed_calls(tmp_path):
    """A denied/failed tool must not record its path — recovery would read
    back files the permission gates refused to touch."""
    (tmp_path / "ok.py").write_text("ok content", encoding="utf-8")
    f = tmp_path / "secret.txt"
    f.write_text("private", encoding="utf-8")
    msgs = [
        _u("edit"),
        assistant_message(
            [
                ContentBlock(type="tool_use", id="ok1", name="Edit", input={"file_path": "ok.py"}),
                ContentBlock(type="tool_use", id="den1", name="Edit", input={"file_path": "secret.txt"}),
                ContentBlock(type="tool_use", id="pend1", name="Edit", input={"file_path": "pending.py"}),
            ]
        ),
        _tr("ok1", "edited"),
        _tr("den1", "denied", error=True),
        # pend1 has no tool_result (e.g. aborted mid-batch)
    ]
    ops = extract_file_ops(msgs)
    assert ops.modified == ["ok.py"]
    reminder = recovery_reminder_text(ops, tmp_path)
    assert reminder is not None and "private" not in reminder


# ---- §3.8 PTL head-trim (review R1) ----

def test_drop_oldest_turn_skips_summary_and_keeps_real_turns():
    """A compaction summary at the span head is not a user turn: the trim
    must drop a REAL oldest turn, not just the summary (multi-compaction
    sessions — the retry input actually shrinks)."""
    from codesage.engine.compaction import _drop_oldest_turn, summary_message

    span = [
        summary_message("previous summary"),
        user_message("q1"),
        assistant_message("a1"),
        user_message("q2"),
        assistant_message("a2"),
        user_message("q3"),
        assistant_message("a3"),
    ]
    trimmed = _drop_oldest_turn(span)
    assert trimmed is not None
    assert all(m.content != "q1" for m in trimmed)  # real oldest turn dropped
    assert any(m.content == "q2" for m in trimmed)  # next turn kept
    assert trimmed[0].content == "q2"


def test_drop_oldest_turn_none_with_single_real_turn():
    """Fewer than two real user turns: nothing to trim — propagate instead."""
    from codesage.engine.compaction import _drop_oldest_turn, summary_message

    span = [summary_message("s"), user_message("q1"), assistant_message("a1")]
    assert _drop_oldest_turn(span) is None  # summary alone is not a turn
