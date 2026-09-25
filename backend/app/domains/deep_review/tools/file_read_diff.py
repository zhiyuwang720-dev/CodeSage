from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.models import ToolExecutionPayload
from app.contracts.tools import RuntimeTool, ToolExecutionContext
from .base import DeepToolContext, ToolPathError, error_payload, json_payload


class FileReadDiffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    start_line: int = Field(default=1, ge=1, description="One-based line in the diff text, not the head file.")
    max_lines: int = Field(default=120, ge=1, le=400)


class FileReadDiffTool(RuntimeTool):
    name = "file_read_diff"
    description = "Read a bounded, pageable range of the in-memory unified diff for one changed file."

    def __init__(self, context: DeepToolContext):
        self.context = context
        self.input_model = FileReadDiffInput

    def validate_input(self, raw_input: dict) -> FileReadDiffInput:
        # Path authorization is checked in execute so a mistaken model lookup
        # becomes a recoverable tool result instead of aborting the Harness.
        return super().validate_input(raw_input)

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
            lines = patch.splitlines(keepends=True)
            start = parsed_input.start_line - 1
            if start >= len(lines):
                return error_payload(
                    "invalid_line_range", f"Diff has {len(lines)} lines; start_line is beyond its end.",
                    max_bytes=limit,
                )
            max_lines = min(parsed_input.max_lines, self.context.config.max_tool_read_lines)
            selected = lines[start : start + max_lines]
            while selected:
                next_line = start + len(selected) + 1 if start + len(selected) < len(lines) else None
                payload = {
                    "path": path,
                    "patch": "".join(selected),
                    "total_lines": len(lines),
                    "start_line": parsed_input.start_line,
                    "end_line": start + len(selected),
                    "next_start_line": next_line,
                    "truncated": next_line is not None,
                }
                if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= limit:
                    return json_payload(payload, max_bytes=limit)
                selected.pop()
            return error_payload(
                "diff_line_too_large",
                "One diff line exceeds the tool output limit; this text cannot be safely paged.",
                max_bytes=limit,
            )
        except (ToolPathError, ValueError) as exc:
            return error_payload(
                "invalid_path",
                f"{exc}; file_read_diff accepts changed review paths only. Use file_read for unchanged context files.",
                max_bytes=self.context.config.max_tool_output_bytes,
            )
