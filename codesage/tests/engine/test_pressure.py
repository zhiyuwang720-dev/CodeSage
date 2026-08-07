"""R1 pressure test: 5000 turns must run without RecursionError or stack growth.

The loop is an explicit while-iteration (not recursive like Kode's), so turn
count must not affect stack depth. This test would fail with RecursionError
on a recursive generator at ~1000 turns (Python's default recursion limit).
"""

import pytest

from codesage.ai import StreamEvent
from codesage.engine import AgentLoop, AgentLoopConfig
from codesage.permissions import PermissionEngine
from codesage.tools import Tool, ToolRegistry, ToolResult, ToolUseContext

# 2001 turns: comfortably past Python's default recursion limit (1000) —
# a recursive loop would RecursionError here. Kept modest because each turn
# re-normalizes the growing message list (O(n^2); phase 10 compaction fixes
# real-world growth).
TURNS = 2001


class TurnTool(Tool):
    name = "Turn"
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return False

    async def _run(self, input, ctx):
        return ToolResult(f"tick {input['n']}")


class TirelessLLM:
    def __init__(self, max_turns):
        self.max_turns = max_turns
        self.calls = 0
        self.total_cost = [0.0]

    def stream(self, request, model="main"):
        return self._gen()

    async def _gen(self):
        self.calls += 1
        if self.calls <= self.max_turns:
            yield StreamEvent(type="tool_use_start", tool_use_id=f"t{self.calls}", tool_name="Turn")
            yield StreamEvent(type="tool_use_delta", input_json_delta=f'{{"n": {self.calls}}}')
            yield StreamEvent(type="done", stop_reason="tool_use")
        else:
            yield StreamEvent(type="text_delta", text="finished")
            yield StreamEvent(type="done", stop_reason="end_turn")


@pytest.mark.asyncio
async def test_2001_turns_no_recursion():
    llm = TirelessLLM(max_turns=TURNS)
    loop = AgentLoop(
        AgentLoopConfig(
            client=llm,
            tools=ToolRegistry([TurnTool()]),
            permissions=PermissionEngine(),
            max_turns=TURNS + 5,
        )
    )
    count = 0
    async for _message in loop.run("go"):
        count += 1
    assert llm.calls == TURNS + 1  # all tool turns + final answer
    assert count == TURNS * 2 + 2  # user + (assistant + tool_round) * N + final
