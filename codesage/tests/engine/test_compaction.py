"""Compaction core tests (specs/08 §3.5): cut-point boundaries, serialization,
summary generation (mock LLM)."""

import pytest

from codesage.ai import ContentBlock, LLMResponse
from codesage.core import assistant_message, user_message
from codesage.engine.compaction import (
    DEFAULT_RESERVE_TOKENS,
    SUMMARY_MAX_TOKENS_FRACTION,
    _is_legal_cut,
    find_cut_point,
    generate_summary,
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
