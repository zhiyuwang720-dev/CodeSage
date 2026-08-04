"""Session storage tests: append-only JSONL, replay, corruption tolerance."""

import json

from codesage.ai import ContentBlock, Usage
from codesage.core import Session, assistant_message, user_message


def _session(tmp_path, sid="s1") -> Session:
    return Session(sid, tmp_path)


def test_append_and_replay_roundtrip(tmp_path):
    session = _session(tmp_path)
    session.append(user_message("你好"))
    session.append(
        assistant_message("答复", usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15))
    )
    messages = session.load()
    assert len(messages) == 2
    assert messages[0].content == "你好"
    assert messages[1].content == "答复"
    assert messages[1].usage.total_tokens == 15


def test_append_is_incremental(tmp_path):
    session = _session(tmp_path)
    session.append(user_message("a"))
    session.append(user_message("b"))
    assert len(session.load()) == 2
    session.append(user_message("c"))
    assert [m.content for m in session.load()] == ["a", "b", "c"]


def test_missing_session_is_empty(tmp_path):
    assert _session(tmp_path, "nope").load() == []
    assert not _session(tmp_path, "nope").exists


def test_corrupt_line_skipped(tmp_path):
    session = _session(tmp_path)
    session.append(user_message("good"))
    with open(session.path, "a", encoding="utf-8") as f:
        f.write("{not json}\n")
        f.write(json.dumps({"role": "user"}) + "\n")  # missing content
    session.append(user_message("still good"))
    assert [m.content for m in session.load()] == ["good", "still good"]


def test_blocks_roundtrip_through_storage(tmp_path):
    session = _session(tmp_path)
    session.append(
        assistant_message([ContentBlock(type="tool_use", id="t1", name="Read", input={"path": "/x"})])
    )
    restored = session.load()[0]
    assert restored.content[0].name == "Read"
    assert restored.content[0].input == {"path": "/x"}
