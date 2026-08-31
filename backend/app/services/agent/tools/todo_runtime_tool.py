from __future__ import annotations

from pydantic import BaseModel, Field

from app.services.review_runtime.models import ToolExecutionPayload
from app.services.runtime_core.interaction_runtime import InteractionRuntime
from app.services.runtime_core.tool_runtime import RuntimeTool, ToolExecutionContext


class TodoWriteInput(BaseModel):
    title: str = Field(..., min_length=1)
    details: str | None = None


class TodoWriteRuntimeTool(RuntimeTool):
    name = "TodoWrite"
    description = "涓哄綋鍓?Agent 鍒涘缓杩愯鏃跺緟鍔為」"
    input_model = TodoWriteInput
    should_defer = True
    always_load = True
    search_hint = "璁板綍寰呭姙鎴栬鍒掓楠?

    def __init__(self, session_store):
        super().__init__()
        self._session_store = session_store
        self._interaction_runtime = InteractionRuntime()

    async def execute(self, parsed_input: TodoWriteInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        runtime_state = self._session_store.load_runtime_state(context.session_id)
        todo = self._interaction_runtime.create_todo(
            runtime_state,
            agent_type=context.agent_type,
            title=parsed_input.title,
            details=parsed_input.details,
        )
        self._session_store.replace_runtime_state(context.session_id, runtime_state)
        return ToolExecutionPayload(
            content=f"Todo recorded: {todo['title']}",
            output_payload={"todo": todo},
            metadata={"interaction": "todo"},
        )
