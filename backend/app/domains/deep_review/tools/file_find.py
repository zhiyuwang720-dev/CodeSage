from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.models import ToolExecutionPayload
from app.contracts.tools import RuntimeTool, ToolExecutionContext
from .base import DeepToolContext, ToolPathError, error_payload, json_payload, run_git_output_bounded


class FileFindInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)


class FileFindTool(RuntimeTool):
    name = "file_find"
    description = "Find tracked repository paths by name or path substring in the fixed head commit."

    def __init__(self, context: DeepToolContext):
        self.context = context
        self.input_model = FileFindInput

    def validate_input(self, raw_input: dict) -> FileFindInput:
        parsed = super().validate_input(raw_input)
        if parsed.query.strip().startswith("-"):
            raise ValueError("query must not start with '-'")
        return parsed

    def is_read_only(self, parsed_input: object = None) -> bool:
        return True

    def is_concurrency_safe(self, parsed_input: object = None) -> bool:
        return True

    def interrupt_behavior(self):
        return "cancel"

    def execution_timeout_seconds(self, parsed_input: object = None, context: object = None) -> float | None:
        return float(self.context.config.tool_timeout_seconds)

    async def execute(self, parsed_input: FileFindInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        try:
            result = await run_git_output_bounded(
                self.context.repo_path,
                ["ls-tree", "-r", "--name-only", "-z", self.context.head_commit],
                max_bytes=self.context.config.max_tool_output_bytes,
                timeout_seconds=self.context.config.tool_timeout_seconds,
            )
            if result.returncode != 0:
                return error_payload("git_failed", "file_find could not read the head tree.")
            query = parsed_input.query.lower()
            matches = sorted(path for path in result.stdout.split("\0") if query in path.lower())
            allowed = [path for path in matches if self.context.is_permitted_context_path(path)]
            limit = self.context.config.max_search_results
            selected: list[str] = []
            used = 0
            for path in allowed:
                encoded = len(path.encode("utf-8")) + 1
                if used + encoded > self.context.config.max_tool_output_bytes:
                    break
                selected.append(path)
                used += encoded
            return json_payload({
                "paths": selected[:limit],
                "truncated": result.truncated or len(allowed) > limit or len(selected) < len(allowed),
            }, max_bytes=self.context.config.max_tool_output_bytes)
        except (ToolPathError, ValueError) as exc:
            return error_payload("invalid_query", str(exc), max_bytes=self.context.config.max_tool_output_bytes)
        except (OSError, TimeoutError, asyncio.TimeoutError):
            return error_payload("tool_timeout", "file_find timed out.")
