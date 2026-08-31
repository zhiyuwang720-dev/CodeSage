from __future__ import annotations

from pydantic import BaseModel, Field

from app.services.review_runtime.models import ToolExecutionPayload
from app.services.runtime_core.interaction_runtime import InteractionRuntime
from app.services.runtime_core.tool_runtime import RuntimeTool, ToolExecutionContext


class AskUserInput(BaseModel):
    question: str = Field(..., min_length=1)
    context: dict[str, str] = Field(default_factory=dict)


class AskUserRuntimeTool(RuntimeTool):
    name = "AskUser"
    description = "Record a runtime question that requires human input"
    input_model = AskUserInput
    should_defer = True
    always_load = True
    search_hint = "ask the user a question"

    def requires_user_interaction(self) -> bool:
        return True

    def __init__(self, session_store):
        super().__init__()
        self._session_store = session_store
        self._interaction_runtime = InteractionRuntime()

    async def execute(self, parsed_input: AskUserInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        runtime_state = self._session_store.load_runtime_state(context.session_id)
        question = self._interaction_runtime.ask_user(
            runtime_state,
            agent_type=context.agent_type,
            question=parsed_input.question,
            context=dict(parsed_input.context or {}),
        )
        self._session_store.replace_runtime_state(context.session_id, runtime_state)
        return ToolExecutionPayload(
            content=f"Question recorded: {question['question']}",
            output_payload={"question": question},
            metadata={"interaction": "ask_user"},
        )
