"""SessionMessage tests: factories, serialization roundtrip."""

from codesage.ai import ContentBlock, Usage
from codesage.core import SessionMessage, assistant_message, user_message


def test_factories():
    assert user_message("hi").role == "user"
    m = assistant_message("answer", usage=Usage(input_tokens=1, output_tokens=2, total_tokens=3), model="m")
    assert m.role == "assistant"
    assert m.usage.output_tokens == 2
    assert m.model == "m"


def test_uuid_unique_and_stable_through_roundtrip():
    m1 = user_message("a")
    m2 = user_message("b")
    assert m1.uuid != m2.uuid
    restored = SessionMessage.from_dict(m1.to_dict())
    assert restored.uuid == m1.uuid


def test_roundtrip_with_blocks_and_usage():
    original = assistant_message(
        [ContentBlock(type="thinking", text="hmm"), ContentBlock(type="text", text="done")],
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        model="deepseek-v4-flash",
    )
    restored = SessionMessage.from_dict(original.to_dict())
    assert restored.content == original.content
    assert restored.usage == original.usage
    assert restored.model == original.model


def test_roundtrip_unicode():
    restored = SessionMessage.from_dict(user_message("你好,世界 🌍").to_dict())
    assert restored.content == "你好,世界 🌍"


def test_to_ai_message():
    m = user_message("hi")
    ai = m.to_ai_message()
    assert ai.role == "user" and ai.content == "hi"


def test_is_meta_flag_roundtrip():
    m = assistant_message("notice", is_meta=True)
    assert SessionMessage.from_dict(m.to_dict()).is_meta
