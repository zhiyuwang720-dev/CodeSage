import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.config import settings
from app.services.agent.agents.base import AgentConfig, AgentResult, AgentType, BaseAgent
from app.services.agent.agents.orchestrator import OrchestratorAgent


class _CollectingEmitter:
    def __init__(self):
        self.events = []

    async def emit(self, event_data):
        self.events.append(event_data)


class _StreamingLLM:
    def get_agent_timeout_config(self):
        return {
            "llm_first_token_timeout": 1,
            "llm_stream_timeout": 1,
            "agent_timeout": 10,
            "sub_agent_timeout": 10,
            "tool_timeout": 10,
        }

    async def chat_completion_stream(self, **kwargs):
        accumulated = ""
        for token in ["a", "b", "c", "d", "e"]:
            accumulated += token
            yield {"type": "token", "content": token, "accumulated": accumulated}
        yield {"type": "done", "content": accumulated, "usage": {"total_tokens": 5}}


class _TestAgent(BaseAgent):
    async def run(self, input_data):
        return AgentResult(success=True)


class _StaticResultAgent:
    def __init__(self, result):
        self.result = result

    async def run(self, input_data):
        return self.result


@pytest.mark.asyncio
async def test_stream_llm_call_batches_thinking_token_events(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_TOKEN_EVENT_CHUNK_SIZE", 2)
    monkeypatch.setattr(settings, "AGENT_TOKEN_EVENT_FLUSH_INTERVAL_MS", 60_000)
    emitter = _CollectingEmitter()
    agent = _TestAgent(
        config=AgentConfig(name="Test", agent_type=AgentType.ANALYSIS),
        llm_service=_StreamingLLM(),
        tools={},
        event_emitter=emitter,
    )

    content, total_tokens = await agent.stream_llm_call([{"role": "user", "content": "hi"}])

    token_events = [event for event in emitter.events if event.event_type == "thinking_token"]

    assert content == "abcde"
    assert total_tokens == 5
    assert [event.metadata["token"] for event in token_events] == ["ab", "cd", "e"]
    assert all("accumulated" not in event.metadata for event in token_events)

    usage_events = [event for event in emitter.events if event.event_type == "llm_usage"]
    assert len(usage_events) == 1
    assert usage_events[0].tokens_used == 5
    assert usage_events[0].metadata["tokens_used"] == 5


@pytest.mark.asyncio
async def test_emit_llm_complete_sets_event_tokens_used():
    emitter = _CollectingEmitter()
    agent = _TestAgent(
        config=AgentConfig(name="Test", agent_type=AgentType.ANALYSIS),
        llm_service=_StreamingLLM(),
        tools={},
        event_emitter=emitter,
    )

    await agent.emit_llm_complete("done", 6612)

    event = emitter.events[-1]
    assert event.event_type == "llm_complete"
    assert event.tokens_used == 6612
    assert event.metadata["tokens_used"] == 6612


def test_agent_stats_expose_current_tokens_used():
    agent = _TestAgent(
        config=AgentConfig(name="Test", agent_type=AgentType.ANALYSIS),
        llm_service=_StreamingLLM(),
        tools={},
    )
    agent._iteration = 3
    agent._tool_calls = 4
    agent._total_tokens = 6612

    assert agent.get_stats() == {
        "iterations": 3,
        "tool_calls": 4,
        "tokens_used": 6612,
    }


@pytest.mark.asyncio
async def test_orchestrator_result_sums_sub_agent_tokens():
    emitter = _CollectingEmitter()
    orchestrator = OrchestratorAgent(
        llm_service=_StreamingLLM(),
        tools={},
        event_emitter=emitter,
        sub_agents={
            "recon": _StaticResultAgent(
                AgentResult(success=True, data={"summary": "recon"}, tokens_used=100)
            ),
            "finding": _StaticResultAgent(
                AgentResult(success=True, data={"summary": "finding", "findings": []}, tokens_used=200)
            ),
        },
    )

    result = await orchestrator.run(
        {
            "project_info": {"file_count": 1},
            "config": {
                "workflow": {
                    "agentStates": {
                        "scan": {"enabled": False},
                        "triage": {"enabled": False},
                        "verification": {"enabled": False},
                    }
                }
            },
        }
    )

    assert result.success is True
    assert result.tokens_used == 300
