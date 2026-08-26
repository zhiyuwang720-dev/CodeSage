"""demo 链路断言:与 test_decision_log 的工具回合同构,轻量版。

demo 的价值在于「可跑 + 可断言」:追踪输出是给人看的,断言是给
回归用的 —— 演示代码如果会悄悄偏离内核行为,它就失去讲解资格。
"""

import sys
from pathlib import Path

_PACKAGES = Path(__file__).resolve().parents[1]
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PACKAGES))
sys.path.insert(0, str(_ROOT / "cordis-py"))

from demo_agent import run_demo  # noqa: E402


def test_demo_trace():
    events, calls = run_demo(verbose=False)
    types = [e["type"] for e in events]

    # 完整事件序列:工具回合 23 条,逐项一致
    assert types == [
        "agent/inbox/spliced",   # followup 入队
        "turn/start",
        "agent/inbox/spliced",   # 回合认领输入
        "step/start",
        "user/message",
        "request/header",
        "request/context",
        "assistant/chunk",       # 文本增量
        "assistant/chunk",       # 工具调用开始
        "assistant/chunk",       # 工具调用参数增量
        "assistant/chunk",       # usage
        "assistant/chunk",       # finish
        "assistant/message",
        "tool/call",
        "tool/result",
        "step/end",
        "step/start",            # 结果折回 → 第二步骤
        "assistant/chunk",       # 文本增量
        "assistant/chunk",       # usage
        "assistant/chunk",       # finish
        "assistant/message",
        "step/end",
        "turn/end",
    ]

    # 空注册表契约语义:UNKNOWN_TOOL 结构化保真
    tool_result = next(e for e in events if e["type"] == "tool/result")
    assert tool_result["data"]["error"] == {
        "name": "ToolNotFoundError", "code": "UNKNOWN_TOOL",
    }
    assert tool_result["sourceEventSeqs"] == [13]  # 链到 seq=13 的 tool/call

    # 两次模型调用 + 回合 completed
    assert len(calls) == 2
    turn_end = next(e for e in events if e["type"] == "turn/end")
    assert turn_end["data"]["reason"] == {"kind": "completed"}
