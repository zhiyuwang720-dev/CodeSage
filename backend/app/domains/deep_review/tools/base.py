from __future__ import annotations

import json
import asyncio
from dataclasses import dataclass
from typing import NamedTuple

from app.contracts.models import ToolExecutionPayload
from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ChangeType, FileChange
from app.domains.deep_review.services.directory_filter import DirectoryFilter, FilterError, normalize_path
from app.domains.deep_review.services.input_builder import ReviewSnapshot


class ToolPathError(ValueError):
    pass


@dataclass(frozen=True)
class DeepToolContext:
    repo_path: str
    head_commit: str
    snapshot: ReviewSnapshot
    config: DeepReviewConfig

    @property
    def allowed_paths(self) -> set[str]:
        return self.snapshot.allowed_paths

    def is_permitted_context_path(self, path: str) -> bool:
        try:
            normalized = normalize_path(path)
        except FilterError:
            return False
        return not DirectoryFilter(self.config).is_secret_path(normalized)

    def is_permitted_change_path(self, path: str) -> bool:
        return path in self.allowed_paths and path in self.snapshot.diff_by_path

    def validate_path(self, path: str, *, require_change: bool = False) -> str:
        from app.domains.deep_review.services.directory_filter import (
            DirectoryFilter,
            normalize_path,
        )

        try:
            normalized = normalize_path(path)
        except FilterError as exc:
            raise ToolPathError(str(exc)) from exc
        if DirectoryFilter(self.config).is_secret_path(normalized):
            raise ToolPathError("path is outside the filtered review context")
        if require_change and normalized not in self.allowed_paths:
            raise ToolPathError("path is outside the filtered review context")
        if require_change and normalized not in self.snapshot.diff_by_path:
            raise ToolPathError("path has no diff")
        return normalized

    def change_for(self, path: str) -> FileChange | None:
        return next((change for change in self.snapshot.changes if change.path == path), None)

    def deleted_paths(self) -> set[str]:
        return {change.path for change in self.snapshot.changes if change.change_type is ChangeType.DELETED}


def json_payload(
    content: dict, *, is_error: bool = False, max_bytes: int | None = None
) -> ToolExecutionPayload:
    text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    if max_bytes is not None:
        if len(text.encode("utf-8")) > max_bytes:
            content = {"error": "output_too_large"}
            is_error = True
            text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    return ToolExecutionPayload(content=text, output_payload=content, metadata={"deep_review_tool": True}, is_error=is_error)


def error_payload(code: str, message: str, *, max_bytes: int | None = None) -> ToolExecutionPayload:
    return json_payload(
        {"error": code, "message": truncate_utf8(message, 300)},
        is_error=True,
        max_bytes=max_bytes,
    )


class BoundedGitResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    truncated: bool


async def run_git_output_bounded(
    repo_path: str,
    args: list[str],
    *,
    max_bytes: int,
    timeout_seconds: int,
) -> BoundedGitResult:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        repo_path,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def read_limited(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        used = 0
        truncated = False
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            used += len(chunk)
            if used <= max_bytes + 1:
                chunks.append(chunk)
            if used > max_bytes:
                truncated = True
                break
        return b"".join(chunks), truncated

    try:
        (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.wait_for(
            asyncio.gather(
                read_limited(process.stdout),
                read_limited(process.stderr),
            ),
            timeout=timeout_seconds,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except TimeoutError:
            process.kill()
            await process.wait()
        truncated = stdout_truncated or stderr_truncated
        if process.returncode != 0 and truncated:
            return BoundedGitResult(process.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace"), True)
        if truncated:
            return BoundedGitResult(0, stdout.decode("utf-8", "replace"), "", True)
        return BoundedGitResult(process.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace"), False)
    except (TimeoutError, asyncio.TimeoutError):
        process.kill()
        await process.wait()
        raise TimeoutError("git output timeout") from None


def truncate_utf8(value: str, limit: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value
    return raw[:limit].decode("utf-8", errors="ignore")
