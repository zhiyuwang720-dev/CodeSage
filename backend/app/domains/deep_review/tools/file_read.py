from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.models import ToolExecutionPayload
from app.contracts.tools import RuntimeTool, ToolExecutionContext
from .base import DeepToolContext, ToolPathError, error_payload, json_payload, truncate_utf8


class FileReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class FileReadTool(RuntimeTool):
    name = "file_read"
    description = "Read a bounded line range from the fixed head commit."

    def __init__(self, context: DeepToolContext):
        self.context = context
        self.input_model = FileReadInput

    def validate_input(self, raw_input: dict) -> FileReadInput:
        parsed = super().validate_input(raw_input)
        if parsed.end_line is not None and parsed.end_line < parsed.start_line:
            raise ValueError("end_line must be >= start_line")
        self.context.validate_path(parsed.path)
        return parsed

    def is_read_only(self, parsed_input: object = None) -> bool:
        return True

    def is_concurrency_safe(self, parsed_input: object = None) -> bool:
        return True

    def interrupt_behavior(self):
        return "cancel"

    def execution_timeout_seconds(self, parsed_input: object = None, context: object = None) -> float | None:
        return float(self.context.config.tool_timeout_seconds)

    async def execute(self, parsed_input: FileReadInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        try:
            path = self.context.validate_path(parsed_input.path)
            if path in self.context.deleted_paths():
                return error_payload("unavailable_deleted_file", "Use file_read_diff for deleted files.")
            change = self.context.change_for(path)
            if change and change.change_type.name == "BINARY":
                return error_payload("binary_file", "Text file_read is unavailable for binary files.")
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", self.context.repo_path, "show", f"{self.context.head_commit}:{path}"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self.context.config.tool_timeout_seconds, check=False,
            )
            if result.returncode != 0:
                return error_payload("head_file_unavailable", "The file does not exist in the fixed head commit.")
            raw = result.stdout.encode("utf-8")
            if "\x00" in result.stdout:
                return error_payload("binary_file", "file_read is unavailable for binary files.")
            if len(raw) > self.context.config.max_file_bytes:
                return error_payload("file_too_large", "The head file exceeds max_file_bytes.")
            lines = result.stdout.splitlines()
            start_index = max(0, parsed_input.start_line - 1)
            requested_end = parsed_input.end_line or (start_index + self.context.config.max_tool_read_lines)
            end_index = min(len(lines), requested_end)
            selected = lines[start_index:end_index]
            truncated = end_index - start_index >= self.context.config.max_tool_read_lines and end_index < len(lines)
            output_limit = self.context.config.max_tool_output_bytes
            rendered: list[str] = []
            used = 0
            for offset, line in enumerate(selected):
                rendered_line = f"{start_index + offset + 1}|{line}"
                encoded = len(rendered_line.encode("utf-8")) + (1 if rendered else 0)
                if used + encoded > output_limit:
                    selected = selected[:offset]
                    truncated = True
                    break
                rendered.append(rendered_line)
                used += encoded
            body = "\n".join(rendered)
            return json_payload({
                "path": path,
                "total_lines": len(lines),
                "start_line": start_index + 1,
                "end_line": start_index + len(selected),
                "truncated": truncated,
                "content": truncate_utf8(body, output_limit),
            }, max_bytes=self.context.config.max_tool_output_bytes)
        except (ToolPathError, ValueError) as exc:
            return error_payload("invalid_path", str(exc), max_bytes=self.context.config.max_tool_output_bytes)
        except (OSError, subprocess.TimeoutExpired):
            return error_payload(
                "tool_timeout", "file_read timed out or failed.", max_bytes=self.context.config.max_tool_output_bytes
            )
