"""Schema-defined terminal review tool for deep review harnesses."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from app.contracts.models import ToolExecutionPayload
from app.tool_gateway.runtime import RuntimeTool, ToolExecutionContext


class InvalidSchemaFinalizeInput:
    def __init__(self, raw_input: dict[str, Any], validation_error: ValidationError):
        self.raw_input = dict(raw_input or {})
        self.validation_error = validation_error


def format_pydantic_validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc") or []) or "input"
        errors.append(
            {
                "loc": location,
                "msg": str(error.get("msg") or "invalid value"),
                "type": str(error.get("type") or "invalid"),
            }
        )
    return errors


class SchemaFinalizeReviewTool(RuntimeTool):
    name = "FinalizeReview"
    always_load = True

    def __init__(self, schema: type[BaseModel]):
        self.schema = schema
        self.input_model = schema
        self.description = (
            "Submit the final structured result exactly as required by the current JSON Schema. "
            "This is a terminal tool: a successful call immediately ends the harness."
        )

    def validate_input(self, raw_input: dict[str, Any]) -> Any:
        try:
            return self.schema.model_validate(raw_input or {})
        except ValidationError as exc:
            return InvalidSchemaFinalizeInput(raw_input or {}, exc)

    def is_concurrency_safe(self, parsed_input: Any = None) -> bool:
        del parsed_input
        return False

    async def execute(
        self,
        parsed_input: BaseModel | InvalidSchemaFinalizeInput,
        context: ToolExecutionContext,
    ) -> ToolExecutionPayload:
        del context
        if isinstance(parsed_input, InvalidSchemaFinalizeInput):
            return ToolExecutionPayload(
                content=(
                    "FinalizeReview rejected this submission. Fix validation_errors and call "
                    "FinalizeReview again with a complete object."
                ),
                output_payload={
                    "finalization_rejected": True,
                    "validation_errors": format_pydantic_validation_errors(
                        parsed_input.validation_error
                    ),
                },
                metadata={"finalization_rejected": True},
            )

        return ToolExecutionPayload(
            content="Received the final structured result.",
            output_payload={
                "final_payload": parsed_input.model_dump(
                    mode="json",
                    exclude_none=True,
                ),
                "completion_mode": "finalize_tool",
                "terminal_action": "finalize_review",
            },
            metadata={"finalize_review": True},
        )
