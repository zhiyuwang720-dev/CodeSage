from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.models import ToolExecutionPayload
from app.contracts.tools import RuntimeTool, ToolExecutionContext
from .base import DeepToolContext, ToolPathError, error_payload, json_payload, run_git_output_bounded


class CodeSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)
    is_regex: bool = False
    path_prefix: str | None = None


class CodeSearchTool(RuntimeTool):
    name = "code_search"
    description = "Search tracked code in the fixed head commit with bounded results."

    def __init__(self, context: DeepToolContext):
        self.context = context
        self.input_model = CodeSearchInput

    def validate_input(self, raw_input: dict) -> CodeSearchInput:
        parsed = super().validate_input(raw_input)
        if not parsed.query.strip() or parsed.query.strip().startswith("-"):
            raise ValueError("query must be non-empty and must not start with '-'")
        if parsed.path_prefix:
            self.context.validate_path(parsed.path_prefix)
        return parsed

    def is_read_only(self, parsed_input: object = None) -> bool:
        return True

    def is_concurrency_safe(self, parsed_input: object = None) -> bool:
        return True

    def interrupt_behavior(self):
        return "cancel"

    def execution_timeout_seconds(self, parsed_input: object = None, context: object = None) -> float | None:
        return float(self.context.config.tool_timeout_seconds)

    async def execute(self, parsed_input: CodeSearchInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        args = ["grep", "-n", "-I", "--no-color"]
        args.append("-E" if parsed_input.is_regex else "-F")
        args.extend(
            [
                "--max-count",
                str(self.context.config.max_search_results + 1),
                "-e",
                parsed_input.query,
                self.context.head_commit,
            ]
        )
        if parsed_input.path_prefix:
            args.extend(["--", parsed_input.path_prefix])
        try:
            result = await run_git_output_bounded(
                self.context.repo_path,
                args,
                max_bytes=self.context.config.max_tool_output_bytes,
                timeout_seconds=self.context.config.tool_timeout_seconds,
            )
            if result.returncode not in {0, 1}:
                return error_payload("git_failed", "code_search could not search the head commit.")
            matches = []
            for line in result.stdout.splitlines():
                try:
                    without_ref = line.partition(":")[2]
                    path, number_text, line_text = without_ref.split(":", 2)
                    number = int(number_text)
                except (ValueError, IndexError):
                    continue
                if self.context.is_permitted_context_path(path):
                    matches.append({"path": path, "line": number, "text": line_text})
            matches.sort(key=lambda item: (item["path"], item["line"]))
            limit = self.context.config.max_search_results
            selected: list[dict] = []
            used = 0
            for match in matches:
                encoded = len((match["path"] + match["text"]).encode("utf-8")) + 8
                if used + encoded > self.context.config.max_tool_output_bytes:
                    break
                selected.append(match)
                used += encoded
            return json_payload({
                "matches": selected[:limit],
                "truncated": result.truncated or len(matches) > limit or len(selected) < len(matches),
            }, max_bytes=self.context.config.max_tool_output_bytes)
        except (ToolPathError, ValueError) as exc:
            return error_payload("invalid_query", str(exc), max_bytes=self.context.config.max_tool_output_bytes)
        except (OSError, TimeoutError, asyncio.TimeoutError):
            return error_payload("tool_timeout", "code_search timed out.")
