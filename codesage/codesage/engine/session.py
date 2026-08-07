"""AgentSession (phase 06): single-run shell around AgentLoop (CC submitMessage).

AgentLoop.run = query: the core loop, streaming messages. AgentSession.submit
= submitMessage: the outer shell that collects a run and returns a
machine-readable RunSummary — no rendering. The REPL's streamed path
(run_single_turn) shares the extraction tail via _summarize_run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .loop import AgentLoop, AgentLoopConfig


@dataclass
class RunSummary:
    """Machine-readable outcome of one single-turn run (--output-format json)."""

    session_id: str
    result: str
    num_turns: int
    usage: int
    total_cost_usd: float
    is_error: bool
    duration_seconds: float
    budget_exceeded: bool = False
    max_turns_exceeded: bool = False
    permission_denials: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "result": self.result,
            "num_turns": self.num_turns,
            "usage": self.usage,
            "total_cost_usd": self.total_cost_usd,
            "is_error": self.is_error,
            "duration_seconds": self.duration_seconds,
            "max_turns_exceeded": self.max_turns_exceeded,
            "budget_exceeded": self.budget_exceeded,
            "permission_denials": self.permission_denials,
        }


def _summarize_run(
    loop: AgentLoop,
    *,
    started: float,
    last_text: str,
    has_error: bool,
    llm_calls: int,
    total_tokens: int,
) -> RunSummary:
    """Shared extraction tail for submit() and run_single_turn().

    All reads are getattr-defensive: test fakes (FakeLoop) implement only the
    run()/message shape. is_error mirrors the old bool (exit code 1),
    budget_exceeded/max_turns_exceeded flag the engine's structured stop
    reason (exit code 1).
    """
    client = getattr(loop, "client", None)
    # CC-10: prefer the engine's structured stop reason over text sniffing.
    stop_reason = getattr(loop, "last_stop_reason", None)
    budget_exceeded = stop_reason == "max_budget"
    max_turns_exceeded = stop_reason == "max_turns"
    if stop_reason is None:
        # AgentLoop.last_stop_reason not landed yet — legacy fallback: text
        # sniff + cost check (remove once the engine sets the reason).
        budget_exceeded = "budget" in last_text.lower() or (
            getattr(loop, "max_budget_usd", None) is not None
            and getattr(client, "total_cost", None) is not None
            and client.total_cost[0] >= loop.max_budget_usd
        )
    return RunSummary(
        session_id=loop.session.path.stem if getattr(loop, "session", None) is not None else "",
        result=last_text,
        num_turns=max(llm_calls, 1),
        usage=total_tokens,
        total_cost_usd=float(getattr(client, "total_cost", [0.0])[0]),
        is_error=has_error,
        duration_seconds=time.monotonic() - started,
        budget_exceeded=budget_exceeded,
        max_turns_exceeded=max_turns_exceeded,
        permission_denials=list(getattr(loop, "last_permission_denials", [])),
    )


class AgentSession:
    """One submitMessage shell: inject a loop (or build one from config),
    submit one input, get the RunSummary back."""

    def __init__(self, loop: AgentLoop):
        self.loop = loop

    @classmethod
    def from_config(cls, config: AgentLoopConfig) -> "AgentSession":
        return cls(AgentLoop(config))

    async def submit(self, user_input: str) -> RunSummary:
        started = time.monotonic()
        has_error = False
        last_text = ""
        llm_calls = 0
        total_tokens = 0
        async for message in self.loop.run(user_input):
            has_error = has_error or (message.role == "assistant" and message.is_error)
            if message.role != "assistant":
                continue
            if message.usage is not None:
                llm_calls += 1
                total_tokens += message.usage.total_tokens
            if isinstance(message.content, str):
                last_text = message.content
            else:
                text = "\n".join(b.text or "" for b in message.content if b.type == "text")
                if text:
                    last_text = text
        return _summarize_run(
            self.loop,
            started=started,
            last_text=last_text,
            has_error=has_error,
            llm_calls=llm_calls,
            total_tokens=total_tokens,
        )
