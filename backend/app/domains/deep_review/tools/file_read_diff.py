from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.contracts.models import ToolExecutionPayload
from app.contracts.tools import RuntimeTool, ToolExecutionContext
from .base import DeepToolContext, ToolPathError, error_payload, json_payload, truncate_utf8


class FileReadDiffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class FileReadDiffTool(RuntimeTool):
    name = "file_read_diff"
    description = "Read the in-memory unified diff for one changed file."

    def __init__(self, context: DeepToolContext):
        self.context = context
        self.input_model = FileReadDiffInput

    def validate_input(self, raw_input: dict) -> FileReadDiffInput:
        parsed = super().validate_input(raw_input)
        self.context.validate_path(parsed.path, require_change=True)
        return parsed

    def is_read_only(self, parsed_input: object = None) -> bool:
        return True

    def is_concurrency_safe(self, parsed_input: object = None) -> bool:
        return True

    def execution_timeout_seconds(self, parsed_input: object = None, context: object = None) -> float | None:
        return float(self.context.config.tool_timeout_seconds)

    def interrupt_behavior(self):
        return "cancel"

    async def execute(self, parsed_input: FileReadDiffInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        try:
            path = self.context.validate_path(parsed_input.path, require_change=True)
            patch = self.context.snapshot.diff_by_path[path]
            limit = self.context.config.max_tool_output_bytes
            raw_patch = patch.encode("utf-8")
            truncated = len(raw_patch) > limit
            content = raw_patch[:limit].decode("utf-8", errors="ignore")
            return json_payload({
                "path": path,
                "patch": content,
                "truncated": truncated,
            }, max_bytes=self.context.config.max_tool_output_bytes)
        except (ToolPathError, ValueError) as exc:
            return error_payload("invalid_path", str(exc), max_bytes=self.context.config.max_tool_output_bytes)
