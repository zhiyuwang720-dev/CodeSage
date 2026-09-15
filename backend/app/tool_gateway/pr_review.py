"""Bounded PR-domain tools backed by a fixed diff and Git snapshot."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.models import ToolExecutionPayload
from app.contracts.tools import RuntimeTool, ToolExecutionContext
from app.domains.pr_review.diff_index import DiffFileIndex, DiffHunkIndex, DiffIndex
from app.infrastructure.repositories.snapshots import GitSnapshotReader, SnapshotError, validate_repository_path


MAX_RESPONSE_BYTES = 16 * 1024


@dataclass(frozen=True)
class PrReviewToolContext:
    run_id: str
    diff_index: DiffIndex
    mode: Literal["diff_only", "repository_required"]
    snapshot_reader: GitSnapshotReader | None = None

    @property
    def source_identity(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "run_id": self.run_id,
            "diff_sha256": self.diff_index.diff_sha256,
            "parser_version": self.diff_index.parser_version,
        }
        if self.snapshot_reader is not None:
            result["snapshot_id"] = self.snapshot_reader.snapshot.snapshot_id
        return result

    @property
    def cursor_key(self) -> bytes:
        return hashlib.sha256(
            f"{self.run_id}:{self.diff_index.diff_sha256}:pr-review-v1".encode()
        ).digest()


def _cursor(context: PrReviewToolContext, payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(context.cursor_key, raw, hashlib.sha256).digest()[:12]
    return base64.urlsafe_b64encode(signature + raw).decode().rstrip("=")


def _decode_cursor(context: PrReviewToolContext, value: str | None, expected: dict[str, Any]) -> int:
    if not value:
        return 0
    try:
        padded = value + "=" * (-len(value) % 4)
        data = base64.urlsafe_b64decode(padded)
        signature, raw = data[:12], data[12:]
        if not hmac.compare_digest(signature, hmac.new(context.cursor_key, raw, hashlib.sha256).digest()[:12]):
            raise ValueError
        payload = json.loads(raw)
        offset = int(payload.pop("offset"))
        if payload != expected or offset < 0:
            raise ValueError
        return offset
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise SnapshotError("cursor_invalid", "cursor does not belong to this query") from exc


def _response(
    context: PrReviewToolContext,
    *,
    status: Literal["ok", "partial", "error"],
    items: list[dict[str, Any]] | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    error_code: str | None = None,
    total_count: int | None = None,
    next_cursor: str | None = None,
    limitations: list[str] | None = None,
) -> ToolExecutionPayload:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": status,
        "source_identity": context.source_identity,
        "items": list(items or []),
        "evidence_refs": list(evidence_refs or []),
        "returned_count": len(items or []),
        "total_count": total_count,
        "truncated": status == "partial" or bool(next_cursor),
        "next_cursor": next_cursor,
        "limitations": list(limitations or []),
    }
    if error_code:
        payload["error_code"] = error_code
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    while len(encoded.encode("utf-8")) > MAX_RESPONSE_BYTES and payload["items"]:
        payload["items"].pop()
        if payload["evidence_refs"]:
            payload["evidence_refs"].pop()
        payload["returned_count"] = len(payload["items"])
        payload["status"] = "partial"
        payload["truncated"] = True
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_RESPONSE_BYTES:
        return ToolExecutionPayload(
            content="PR tool response could not fit the configured bound",
            output_payload={"status": "error", "error_code": "response_too_large"},
            metadata={"error_kind": "response_too_large"},
            is_error=True,
        )
    return ToolExecutionPayload(
        content=encoded,
        output_payload=payload,
        metadata={"pr_review_tool": True, "status": payload["status"]},
        is_error=status == "error",
    )


def _error(context: PrReviewToolContext, exc: SnapshotError) -> ToolExecutionPayload:
    return _response(
        context,
        status="error",
        error_code=exc.code,
        limitations=[str(exc)],
    )


class ListChangesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cursor: str | None = None
    page_size: int = Field(default=20, ge=1, le=50)


class ReadDiffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str
    hunk_id: str | None = None
    cursor: str | None = None
    max_lines: int = Field(default=100, ge=1, le=200)


class SearchDiffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=256)
    file_ids: list[str] | None = None
    cursor: str | None = None
    max_results: int = Field(default=20, ge=1, le=50)


class ReadSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side: Literal["base", "head"]
    path: str
    start_line: int = Field(ge=1)
    max_lines: int = Field(default=80, ge=1, le=200)


class SearchSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side: Literal["base", "head"]
    query: str = Field(min_length=1, max_length=256)
    path_prefix: str | None = None
    cursor: str | None = None
    max_results: int = Field(default=20, ge=1, le=50)


class _PrTool(RuntimeTool):
    def __init__(self, context: PrReviewToolContext):
        self.context = context

    def is_read_only(self, parsed_input: Any = None) -> bool:
        return True

    def is_concurrency_safe(self, parsed_input: Any = None) -> bool:
        return True


class ListChangesTool(_PrTool):
    name = "ListChanges"
    description = "List changed files and hunk identifiers in the fixed review diff."
    input_model = ListChangesInput

    async def execute(self, parsed_input: ListChangesInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        binding = {"tool": self.name, "page_size": parsed_input.page_size}
        try:
            offset = _decode_cursor(self.context, parsed_input.cursor, binding)
        except SnapshotError as exc:
            return _error(self.context, exc)
        files = self.context.diff_index.files
        selected = files[offset : offset + parsed_input.page_size]
        next_offset = offset + len(selected)
        next_cursor = _cursor(self.context, {**binding, "offset": next_offset}) if next_offset < len(files) else None
        return _response(
            self.context,
            status="partial" if next_cursor else "ok",
            total_count=len(files),
            next_cursor=next_cursor,
            items=[
                {
                    "file_id": item.file_id,
                    "old_path": item.old_path,
                    "new_path": item.new_path,
                    "status": item.status,
                    "binary": item.binary,
                    "patch_size_bytes": item.patch_size_bytes,
                    "hunk_ids": [hunk.hunk_id for hunk in item.hunks],
                }
                for item in selected
            ],
        )


def _diff_lines(file: DiffFileIndex, hunk_id: str | None) -> tuple[list[tuple[DiffHunkIndex, Any]], SnapshotError | None]:
    hunks = file.hunks
    if hunk_id:
        hunks = [item for item in hunks if item.hunk_id == hunk_id]
        if not hunks:
            return [], SnapshotError("diff_hunk_not_found", "hunk_id is not present in this file")
    return [(hunk, line) for hunk in hunks for line in hunk.lines], None


class ReadDiffTool(_PrTool):
    name = "ReadDiff"
    description = "Read bounded lines from a changed file or hunk in the fixed review diff."
    input_model = ReadDiffInput

    async def execute(self, parsed_input: ReadDiffInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        file = self.context.diff_index.file(parsed_input.file_id)
        if file is None:
            return _error(self.context, SnapshotError("diff_file_not_found", "file_id is not present in this diff"))
        pairs, error = _diff_lines(file, parsed_input.hunk_id)
        if error:
            return _error(self.context, error)
        binding = {"tool": self.name, "file_id": file.file_id, "hunk_id": parsed_input.hunk_id, "max_lines": parsed_input.max_lines}
        try:
            offset = _decode_cursor(self.context, parsed_input.cursor, binding)
        except SnapshotError as exc:
            return _error(self.context, exc)
        selected = pairs[offset : offset + parsed_input.max_lines]
        next_offset = offset + len(selected)
        next_cursor = _cursor(self.context, {**binding, "offset": next_offset}) if next_offset < len(pairs) else None
        items = [
            {
                "hunk_id": hunk.hunk_id,
                "text": line.text,
                "kind": line.kind,
                "old_line": line.old_line,
                "new_line": line.new_line,
            }
            for hunk, line in selected
        ]
        content = "\n".join(item[1].text for item in selected)
        lines = [item[1].new_line or item[1].old_line for item in selected if item[1].new_line or item[1].old_line]
        evidence = []
        if selected and lines:
            evidence.append(
                {
                    "evidence_id": "evidence-" + hashlib.sha256((file.file_id + content).encode()).hexdigest()[:24],
                    "run_id": self.context.run_id,
                    "kind": "diff",
                    "file_id": file.file_id,
                    "path": file.display_path,
                    "line_start": min(lines),
                    "line_end": max(lines),
                    "hunk_id": parsed_input.hunk_id,
                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "origin": "tool",
                    "producer_version": self.context.diff_index.parser_version,
                }
            )
        return _response(self.context, status="partial" if next_cursor else "ok", items=items, evidence_refs=evidence, total_count=len(pairs), next_cursor=next_cursor)


class SearchDiffTool(_PrTool):
    name = "SearchDiff"
    description = "Search literal text in the fixed review diff. Regex is not accepted."
    input_model = SearchDiffInput

    async def execute(self, parsed_input: SearchDiffInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        allowed = set(parsed_input.file_ids or [])
        if allowed and not allowed.issubset({item.file_id for item in self.context.diff_index.files}):
            return _error(self.context, SnapshotError("diff_file_not_found", "file_ids contains an unknown file"))
        matches: list[dict[str, Any]] = []
        for file in self.context.diff_index.files:
            if allowed and file.file_id not in allowed:
                continue
            for hunk in file.hunks:
                for line in hunk.lines:
                    if parsed_input.query in line.text:
                        matches.append({"file_id": file.file_id, "path": file.display_path, "hunk_id": hunk.hunk_id, "old_line": line.old_line, "new_line": line.new_line, "text": line.text})
        binding = {"tool": self.name, "query_hash": hashlib.sha256(parsed_input.query.encode()).hexdigest(), "file_ids": sorted(allowed), "max_results": parsed_input.max_results}
        try:
            offset = _decode_cursor(self.context, parsed_input.cursor, binding)
        except SnapshotError as exc:
            return _error(self.context, exc)
        selected = matches[offset : offset + parsed_input.max_results]
        next_offset = offset + len(selected)
        next_cursor = _cursor(self.context, {**binding, "offset": next_offset}) if next_offset < len(matches) else None
        return _response(self.context, status="partial" if next_cursor else "ok", items=selected, total_count=len(matches), next_cursor=next_cursor)


class ReadSourceTool(_PrTool):
    name = "ReadSource"
    description = "Read bounded lines from a repository-relative path at the fixed base or head commit."
    input_model = ReadSourceInput

    async def execute(self, parsed_input: ReadSourceInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        reader = self.context.snapshot_reader
        if reader is None:
            return _error(self.context, SnapshotError("source_unavailable", "source capability is not available"))
        try:
            blob = await reader.read_blob(side=parsed_input.side, path=parsed_input.path)
        except SnapshotError as exc:
            return _error(self.context, exc)
        lines = blob.decode("utf-8", errors="replace").splitlines()
        start = parsed_input.start_line - 1
        selected = lines[start : start + parsed_input.max_lines]
        items = [{"path": validate_repository_path(parsed_input.path), "side": parsed_input.side, "line": start + index + 1, "text": text} for index, text in enumerate(selected)]
        content = "\n".join(selected)
        commit = reader.commit_for_side(parsed_input.side)
        evidence = []
        if selected:
            evidence.append({"evidence_id": "evidence-" + hashlib.sha256((commit + parsed_input.path + content).encode()).hexdigest()[:24], "run_id": self.context.run_id, "kind": "source", "snapshot_id": reader.snapshot.snapshot_id, "side": parsed_input.side, "commit_sha": commit, "path": validate_repository_path(parsed_input.path), "line_start": parsed_input.start_line, "line_end": parsed_input.start_line + len(selected) - 1, "content_sha256": hashlib.sha256(content.encode()).hexdigest(), "origin": "tool", "producer_version": reader.snapshot.verification_version})
        next_cursor = str(parsed_input.start_line + len(selected)) if start + len(selected) < len(lines) else None
        return _response(self.context, status="partial" if next_cursor else "ok", items=items, evidence_refs=evidence, total_count=len(lines), next_cursor=next_cursor)


class SearchSourceTool(_PrTool):
    name = "SearchSource"
    description = "Search bounded literal text in text blobs at the fixed base or head commit."
    input_model = SearchSourceInput

    async def execute(self, parsed_input: SearchSourceInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        reader = self.context.snapshot_reader
        if reader is None:
            return _error(self.context, SnapshotError("source_unavailable", "source capability is not available"))
        prefix = ""
        if parsed_input.path_prefix:
            try:
                prefix = validate_repository_path(parsed_input.path_prefix).rstrip("/")
            except SnapshotError as exc:
                return _error(self.context, exc)
        binding = {"tool": self.name, "side": parsed_input.side, "query_hash": hashlib.sha256(parsed_input.query.encode()).hexdigest(), "path_prefix": prefix, "max_results": parsed_input.max_results}
        try:
            offset = _decode_cursor(self.context, parsed_input.cursor, binding)
            paths = [
                path
                for path in await reader.list_paths(side=parsed_input.side)
                if not prefix or path == prefix or path.startswith(prefix + "/")
            ]
        except SnapshotError as exc:
            return _error(self.context, exc)
        matches: list[dict[str, Any]] = []
        scanned_bytes = 0
        scanned_files = 0
        index = offset
        while index < len(paths) and scanned_files < 500 and scanned_bytes < 8 * 1024 * 1024 and len(matches) < parsed_input.max_results:
            path = paths[index]
            index += 1
            scanned_files += 1
            try:
                blob = await reader.read_blob(side=parsed_input.side, path=path, max_bytes=8 * 1024 * 1024)
            except SnapshotError:
                continue
            scanned_bytes += len(blob)
            if b"\x00" in blob:
                continue
            for line_number, text in enumerate(blob.decode("utf-8", errors="replace").splitlines(), start=1):
                if parsed_input.query in text:
                    matches.append({"path": path, "side": parsed_input.side, "line": line_number, "text": text})
                    if len(matches) >= parsed_input.max_results:
                        break
        next_cursor = _cursor(self.context, {**binding, "offset": index}) if index < len(paths) else None
        limitations = []
        if next_cursor:
            limitations.append(f"scanned {scanned_files} files/{scanned_bytes} bytes in this page")
        return _response(self.context, status="partial" if next_cursor else "ok", items=matches, total_count=None, next_cursor=next_cursor, limitations=limitations)


def build_pr_review_tool_catalog(context: PrReviewToolContext) -> list[RuntimeTool]:
    tools: list[RuntimeTool] = [
        ListChangesTool(context),
        ReadDiffTool(context),
        SearchDiffTool(context),
    ]
    if context.mode == "repository_required" and context.snapshot_reader is not None:
        tools.extend([ReadSourceTool(context), SearchSourceTool(context)])
    return tools
