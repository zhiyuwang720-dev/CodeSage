"""token 计量测试:固定密度估算纯函数 + usage 聚合桶。

运行:python -m pytest tests/ -q
前置与 test_llm.py 相同:自行插入 sys.path。
"""

import asyncio
import sys
from pathlib import Path

_FAMILY = Path(__file__).resolve().parents[2]  # 家族根 packages/llm
_ROOT = Path(__file__).resolve().parents[4]  # 仓库根(CodeSage)
sys.path.insert(0, str(_FAMILY))
sys.path.insert(0, str(_ROOT / "cordis-py"))

from cordis import Context  # noqa: E402

from llm import (  # noqa: E402
    BLOCK_OVERHEAD,
    CHARS_PER_TOKEN,
    ROLE_OVERHEAD,
    ContentBlock,
    LLMRequest,
    Message,
    TokenMeter,
    Usage,
    estimate_content,
    estimate_message,
    estimate_request,
    estimate_system_tokens,
    estimate_text,
    usage_tokens,
)


def test_estimate_text_density():
    # 4 字符折 1 token,再加块开销
    assert estimate_text("abcd") == 1 + BLOCK_OVERHEAD
    assert estimate_text("a" * (CHARS_PER_TOKEN * 10)) == 10 + BLOCK_OVERHEAD
    assert estimate_text("") == BLOCK_OVERHEAD
    assert estimate_text("ab") == 1 + BLOCK_OVERHEAD  # 向上取整


def test_estimate_content_by_block_type():
    blocks = [
        ContentBlock(type="text", text="abcd"),
        ContentBlock(type="thinking", text="efgh"),
        ContentBlock(
            type="tool_use", id="t1", name="bash", input={"cmd": "ls"}
        ),
        ContentBlock(
            type="tool_result",
            tool_use_id="t1",
            content=[ContentBlock(type="text", text="file1")],
        ),
    ]
    expected = (
        estimate_text("abcd")
        + estimate_text("efgh")
        + estimate_text("bash")
        + estimate_text('{"cmd": "ls"}')
        + estimate_text("file1")
        + BLOCK_OVERHEAD  # tool_result 载体开销
    )
    assert estimate_content(blocks) == expected


def test_estimate_content_string_tool_result():
    # tool_result 的内容也可能是纯字符串,按文本计价
    blocks = [ContentBlock(type="tool_result", tool_use_id="t1", content="ok")]
    assert estimate_content(blocks) == estimate_text("ok") + BLOCK_OVERHEAD


def test_estimate_message_role_overhead():
    message = Message(role="user", content="abcd")
    assert estimate_message(message) == estimate_text("abcd") + ROLE_OVERHEAD
    rich = Message(
        role="assistant",
        content=[ContentBlock(type="text", text="abcd")],
    )
    assert estimate_message(rich) == estimate_text("abcd") + ROLE_OVERHEAD


def test_estimate_request_includes_system():
    request = LLMRequest(
        messages=[
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi"),
        ],
        system="system prompt",
    )
    assert estimate_request(request) == (
        estimate_system_tokens("system prompt")
        + estimate_message(request.messages[0])
        + estimate_message(request.messages[1])
    )
    # 无系统提示:计 0
    request.system = None
    assert estimate_request(request) == sum(
        estimate_message(m) for m in request.messages
    )


def test_usage_tokens_sums_all_four():
    usage = Usage(
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=7,
        cache_write_tokens=3,
    )
    assert usage_tokens(usage) == 25


def test_usage_bucket_merge_and_total():
    meter_ctx = Context()
    meter = TokenMeter(meter_ctx)
    assert meter.name == "token-meter"

    bucket = meter.record(
        Usage(input_tokens=10, output_tokens=5), provider="fake", model="m1"
    )
    meter.record(
        Usage(input_tokens=3, output_tokens=2), provider="fake", model="m1"
    )
    meter.record(
        Usage(input_tokens=1, output_tokens=1), provider="fake", model="m2"
    )
    assert bucket.total_tokens == 20
    assert len(meter.buckets()) == 2
    assert meter.total_tokens() == 22

    # 桶的聚合是纯增量:重复记同一次 usage 会累计(调用方负责去重)
    meter.record(Usage(input_tokens=1, output_tokens=1), provider="fake", model="m2")
    assert meter.total_tokens() == 24
