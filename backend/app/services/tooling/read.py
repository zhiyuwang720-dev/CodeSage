"""文件读取/搜索工具族(P4 归一: 原 file_tool 四工具的直接 RuntimeTool 实现)。

Read(单文件/批量读取)、Glob(枚举)、Grep(内容搜索)三个工具直接做底层文件操作并返回
ToolExecutionPayload, 不再经过旧工具壳/双写。输入沿用 canonical 字段集
(ReadToolInput/GlobToolInput/GrepToolInput), 工程约束(project_root/exclude/target/
additional_roots)在构造期注入, 与旧 file_tool 语义一致。
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.services.contracts.models import ToolExecutionPayload
from app.services.tooling.runtime import (
    RUNTIME_SEARCH_TOOL_MAX_TIMEOUT_SECONDS,
    RUNTIME_SEARCH_TOOL_TIMEOUT_SECONDS,
    RuntimeTool,
    ToolExecutionContext,
)

GLOB_DEFAULT_MAX_RESULTS = 100
GLOB_HARD_MAX_RESULTS = 100
GREP_DEFAULT_MAX_RESULTS = 250
GREP_HARD_MAX_RESULTS = 250
TRUNCATED_RESULT_HINT = "结果被截断，使用更具体的 path 或 pattern。"

DEFAULT_EXCLUDE_DIRS = {
    "node_modules",
    "vendor",
    "dist",
    "build",
    ".git",
    "__pycache__",
    ".pytest_cache",
    "coverage",
    ".nyc_output",
    ".vscode",
    ".idea",
    ".vs",
    ".cache",
    ".next",
    ".nuxt",
    ".parcel-cache",
    ".pnpm-store",
    ".svelte-kit",
    ".turbo",
    "bower_components",
    "logs",
    "out",
    "target",
    "tmp",
    "temp",
    "venv",
    "env",
}


class ReadToolInput(BaseModel):
    file_path: str | None = Field(default=None, description="Path to a file relative to the project root.")
    file_paths: list[str] = Field(default_factory=list, description="Optional batch of related files to read together.")
    start_line: int | None = Field(default=None, description="Optional 1-based start line.")
    end_line: int | None = Field(default=None, description="Optional inclusive end line.")
    max_lines: int = Field(default=400, description="Maximum lines to return per file.")
    max_files: int = Field(default=6, description="Maximum files when batch reading.")


class GlobToolInput(BaseModel):
    path: str = Field(default=".", description="Directory relative to the project root.")
    pattern: str | None = Field(default=None, description="Optional glob pattern, for example **/*.java or *.xml.")
    recursive: bool = Field(default=True, description="Whether to walk child directories.")
    max_results: int = Field(default=GLOB_DEFAULT_MAX_RESULTS, description="Maximum files to return.")
    timeout_seconds: int = Field(default=RUNTIME_SEARCH_TOOL_TIMEOUT_SECONDS, ge=1, le=RUNTIME_SEARCH_TOOL_MAX_TIMEOUT_SECONDS)


class GrepToolInput(BaseModel):
    pattern: str = Field(description="Keyword or regular expression to search for.")
    path: str | None = Field(default=None, description="Optional directory relative to the project root.")
    glob: str | None = Field(default=None, description="Optional glob such as *.py or **/*.java.")
    case_sensitive: bool = Field(default=False, description="Whether the search is case sensitive.")
    max_results: int = Field(default=GREP_DEFAULT_MAX_RESULTS, description="Maximum number of matches to return.")
    is_regex: bool = Field(default=False, description="Whether pattern should be treated as regex.")
    timeout_seconds: int = Field(default=RUNTIME_SEARCH_TOOL_TIMEOUT_SECONDS, ge=1, le=RUNTIME_SEARCH_TOOL_MAX_TIMEOUT_SECONDS)


def _build_allowed_roots(project_root: str, additional_roots: list[str] | None = None) -> list[str]:
    roots: list[str] = []
    for raw_root in [project_root, *(additional_roots or [])]:
        normalized = os.path.realpath(str(raw_root or "").strip())
        if normalized and normalized not in roots:
            roots.append(normalized)
    return roots


def _resolve_allowed_path(path_value: str, allowed_roots: list[str]) -> str | None:
    raw_path = str(path_value or "").strip()
    if not raw_path:
        return None

    if os.path.isabs(raw_path):
        candidate = os.path.realpath(raw_path)
        if any(candidate.startswith(root) for root in allowed_roots):
            return candidate
        return None

    fallback_candidate: str | None = None
    for root in allowed_roots:
        candidate = os.path.realpath(os.path.join(root, raw_path))
        if not candidate.startswith(root):
            continue
        if os.path.exists(candidate):
            return candidate
        if fallback_candidate is None:
            fallback_candidate = candidate
    return fallback_candidate


def _best_display_path(full_path: str, project_root: str, allowed_roots: list[str], requested_path: str) -> str:
    if not full_path:
        return requested_path

    normalized_full = os.path.realpath(full_path)
    normalized_project = os.path.realpath(project_root)
    if normalized_full.startswith(normalized_project):
        return os.path.relpath(normalized_full, normalized_project).replace("\\", "/")

    for root in allowed_roots[1:]:
        normalized_root = os.path.realpath(root)
        if normalized_full.startswith(normalized_root):
            root_name = os.path.basename(normalized_root.rstrip("/\\"))
            relative = os.path.relpath(normalized_full, normalized_root).replace("\\", "/")
            return f"{root_name}/{relative}" if relative != "." else root_name

    return requested_path.replace("\\", "/")


def _detect_language(file_path: str) -> str:
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".java": "java",
        ".go": "go",
        ".rs": "rust",
        ".cpp": "cpp",
        ".c": "c",
        ".cs": "csharp",
        ".php": "php",
        ".rb": "ruby",
        ".swift": "swift",
        ".md": "markdown",
        ".xml": "xml",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".json": "json",
    }.get(Path(file_path).suffix.lower(), "text")


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate rg promptly on result limit, timeout, or task cancellation."""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=1.5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


def _rg_exclude_args(exclude_dirs: set[str], exclude_patterns: list[str]) -> list[str]:
    args: list[str] = []
    for directory in sorted(exclude_dirs):
        args.extend(["--glob", f"!**/{directory}/**"])
    for pattern in exclude_patterns:
        normalized = str(pattern or "").strip().replace("\\", "/")
        if normalized:
            args.extend(["--glob", f"!{normalized}"])
    return args


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        resolved = default
    return max(1, resolved)


def _append_truncation_hint(payload: ToolExecutionPayload, *, requested: int, limit: int) -> ToolExecutionPayload:
    if requested <= limit:
        return payload
    content = payload.content.rstrip()
    if TRUNCATED_RESULT_HINT not in content:
        content = f"{content}\n\n{TRUNCATED_RESULT_HINT}" if content else TRUNCATED_RESULT_HINT
    metadata = dict(payload.metadata or {})
    metadata.update(
        {
            "truncated": True,
            "requested_limit": requested,
            "applied_limit": limit,
        }
    )
    output_payload = dict(payload.output_payload or {})
    output_payload.update(
        {
            "truncated": True,
            "requested_limit": requested,
            "applied_limit": limit,
            "truncation_hint": TRUNCATED_RESULT_HINT,
        }
    )
    return ToolExecutionPayload(
        content=content,
        output_payload=output_payload,
        metadata=metadata,
        is_error=payload.is_error,
        context_modifier=payload.context_modifier,
    )


class _FileScopeMixin:
    """共享工程作用域(project_root/allowed_roots/exclude/target), 对应旧 file_tool 构造逻辑。"""

    def _init_file_scope(
        self,
        *,
        project_root: str,
        exclude_patterns: list[str] | None,
        target_files: list[str] | None,
        additional_roots: list[str] | None,
    ) -> None:
        self.project_root = os.path.realpath(str(project_root or "").strip() or ".")
        self.exclude_patterns = list(exclude_patterns or [])
        self.target_files = set(target_files) if target_files else None
        self.additional_roots = [str(root) for root in (additional_roots or []) if str(root or "").strip()]
        self.allowed_roots = _build_allowed_roots(self.project_root, self.additional_roots)
        self.exclude_dirs = set(DEFAULT_EXCLUDE_DIRS)
        for pattern in self.exclude_patterns:
            if pattern.endswith("/**"):
                self.exclude_dirs.add(pattern[:-3])
            elif "/" not in pattern and "*" not in pattern:
                self.exclude_dirs.add(pattern)


class ReadRuntimeTool(_FileScopeMixin, RuntimeTool):
    """Read: 单文件或批量读取(原 FileReadTool/ReadManyFilesTool 的 RuntimeTool 直实现)。"""

    name = "Read"
    description = (
        "读取项目本地文件内容。适合查看源代码、路由、控制器、服务、模型、配置、SQL、XML、模板、"
        "测试文件、依赖文件以及 Skill 引用文档。\n\n"
        "用法：\n"
        "- file_path 为相对项目根目录的文件路径。\n"
        "- 也可以使用 file_paths 一次读取少量强相关文件，例如同一个调用链上的 route/controller/service/model。\n"
        "- start_line 和 end_line 用于只读取已知相关片段；不确定位置时先读取完整文件或较大范围。\n"
        "- max_lines 控制单个文件最多返回的行数，长文件建议分段读取。\n"
        "- Read 只能读取文件，不能枚举目录；需要发现文件时先用 Glob，需要按关键字查找时用 Grep。\n\n"
        "审计要求：\n"
        "- 当你需要查看代码、确认实现、补齐 source/sink、验证调用链或提取代码片段时，使用 Read。\n"
        "- 如果你说“继续查看/继续追踪/让我检查/需要读取/确认实现”，必须实际调用 Read、Grep、Glob "
        "或其它合适工具，而不是只描述下一步计划。"
    )
    input_model = ReadToolInput

    def __init__(
        self,
        *,
        project_root: str,
        exclude_patterns: list[str] | None = None,
        target_files: list[str] | None = None,
        additional_roots: list[str] | None = None,
    ):
        self._init_file_scope(
            project_root=project_root,
            exclude_patterns=exclude_patterns,
            target_files=target_files,
            additional_roots=additional_roots,
        )

    def validate_input(self, raw_input: dict[str, Any]) -> ReadToolInput:
        payload = dict(raw_input or {})
        normalized = {
            "file_path": payload.get("file_path") or payload.get("path") or payload.get("file"),
            "file_paths": list(payload.get("file_paths") or payload.get("paths") or []),
            "start_line": payload.get("start_line") or payload.get("from_line"),
            "end_line": payload.get("end_line") or payload.get("to_line"),
            "max_lines": payload.get("max_lines") or payload.get("limit") or 400,
            "max_files": payload.get("max_files") or 6,
        }
        if not normalized["file_path"] and normalized["file_paths"]:
            normalized["file_path"] = normalized["file_paths"][0]
        return ReadToolInput.model_validate(normalized)

    def is_concurrency_safe(self, parsed_input: Any = None) -> bool:
        del parsed_input
        return True

    def is_read_only(self, parsed_input: Any = None) -> bool:
        del parsed_input
        return True

    @staticmethod
    def _read_file_lines_sync(file_path: str, start_idx: int, end_idx: int) -> tuple[list[str], int]:
        selected_lines: list[str] = []
        total_lines = 0
        with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
            for index, line in enumerate(handle):
                total_lines = index + 1
                if start_idx <= index < end_idx:
                    selected_lines.append(line)
                elif index >= end_idx:
                    break
        return selected_lines, total_lines

    @staticmethod
    def _read_all_lines_sync(file_path: str) -> list[str]:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.readlines()

    def _is_target_allowed(self, requested_path: str, resolved_path: str) -> bool:
        if not self.target_files:
            return True
        normalized_project = os.path.realpath(self.project_root)
        if not os.path.realpath(resolved_path).startswith(normalized_project):
            return True
        relative_path = os.path.relpath(resolved_path, normalized_project).replace("\\", "/")
        requested_relative = str(requested_path or "").replace("\\", "/").strip()
        return relative_path in self.target_files or requested_relative in self.target_files

    def _should_exclude(self, display_path: str) -> bool:
        basename = os.path.basename(display_path)
        for pattern in self.exclude_patterns:
            if fnmatch.fnmatch(display_path, pattern) or fnmatch.fnmatch(basename, pattern):
                return True
        return False

    async def _render_resolved_file(
        self,
        *,
        requested_path: str,
        full_path: str,
        start_line: int | None,
        end_line: int | None,
        max_lines: int,
    ) -> tuple[bool, str, dict[str, Any], str | None]:
        """读取单个文件并渲染为 (ok, text_or_message, metadata, error)。"""
        if not os.path.exists(full_path):
            return False, "", {}, f"文件不存在: {requested_path}"
        if not os.path.isfile(full_path):
            return False, "", {}, f"不是文件: {requested_path}"

        file_size = os.path.getsize(full_path)
        is_large_file = file_size > 1024 * 1024
        if is_large_file and start_line is None and end_line is None:
            return (
                False,
                "",
                {},
                f"文件过大 ({file_size / 1024:.1f}KB)，请指定 start_line 和 end_line 读取部分内容",
            )

        if is_large_file and (start_line is not None or end_line is not None):
            start_idx = max(0, (start_line or 1) - 1)
            end_idx = end_line if end_line else start_idx + max_lines
            selected_lines, total_lines = await asyncio.to_thread(
                self._read_file_lines_sync, full_path, start_idx, end_idx
            )
            end_idx = min(end_idx, start_idx + len(selected_lines))
        else:
            lines = await asyncio.to_thread(self._read_all_lines_sync, full_path)
            total_lines = len(lines)
            start_idx = max(0, (start_line or 1) - 1)
            end_idx = min(total_lines, end_line) if end_line is not None else min(total_lines, start_idx + max_lines)
            selected_lines = lines[start_idx:end_idx]

        nul_character_count = sum(line.count("\x00") for line in selected_lines)
        printable_lines = [line.replace("\x00", "\\x00") for line in selected_lines]
        numbered_lines = [
            f"{index:4d}| {line.rstrip()}"
            for index, line in enumerate(printable_lines, start=start_idx + 1)
        ]
        display_path = _best_display_path(full_path, self.project_root, self.allowed_roots, requested_path)
        language = _detect_language(full_path)
        output = f"文件: {display_path}\n"
        output += f"行数: {start_idx + 1}-{end_idx} / {total_lines}\n\n"
        output += f"```{language}\n" + "\n".join(numbered_lines) + "\n```"
        if end_idx < total_lines:
            output += f"\n\n... 还有 {total_lines - end_idx} 行未显示"

        metadata = {
            "file_path": display_path,
            "resolved_path": full_path,
            "total_lines": total_lines,
            "start_line": start_idx + 1,
            "end_line": end_idx,
            "language": language,
            "nul_characters_escaped": nul_character_count,
        }
        return True, output, metadata, None

    async def _read_one(
        self,
        *,
        file_path: str,
        start_line: int | None,
        end_line: int | None,
        max_lines: int,
    ) -> tuple[bool, str, dict[str, Any], str | None]:
        full_path = _resolve_allowed_path(file_path, self.allowed_roots)
        if not full_path:
            return False, "", {}, "安全错误：不允许访问项目目录外的文件"
        if not self._is_target_allowed(file_path, full_path):
            return False, "", {}, f"文件被排除或不在目标文件列表中: {file_path}"

        display_path = _best_display_path(full_path, self.project_root, self.allowed_roots, file_path)
        if self._should_exclude(display_path):
            return False, "", {}, f"文件被排除或不在目标文件列表中: {display_path}"

        try:
            return await self._render_resolved_file(
                requested_path=file_path,
                full_path=full_path,
                start_line=start_line,
                end_line=end_line,
                max_lines=max_lines,
            )
        except Exception as exc:  # noqa: BLE001
            return False, "", {}, f"读取文件失败: {str(exc)}"

    async def execute(self, parsed_input: ReadToolInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        file_paths = [item for item in parsed_input.file_paths if str(item or "").strip()]
        if len(file_paths) > 1:
            return await self._execute_many(
                file_paths=file_paths,
                start_line=parsed_input.start_line,
                end_line=parsed_input.end_line,
                max_lines=parsed_input.max_lines,
                max_files=parsed_input.max_files,
            )

        if not parsed_input.file_path:
            raise ValueError("Read requires file_path or file_paths.")
        ok, text, metadata, error = await self._read_one(
            file_path=parsed_input.file_path,
            start_line=parsed_input.start_line,
            end_line=parsed_input.end_line,
            max_lines=parsed_input.max_lines,
        )
        if not ok:
            return ToolExecutionPayload(
                content=error or "Read failed",
                output_payload=dict(metadata),
                metadata={"success": False, **dict(metadata)},
                is_error=True,
            )
        return ToolExecutionPayload(
            content=text,
            output_payload=dict(metadata),
            metadata={"success": True, **dict(metadata)},
            is_error=False,
        )

    async def _execute_many(
        self,
        *,
        file_paths: list[str],
        start_line: int | None,
        end_line: int | None,
        max_lines: int,
        max_files: int,
    ) -> ToolExecutionPayload:
        normalized_paths: list[str] = []
        seen = set()
        for raw_path in file_paths:
            file_path = str(raw_path or "").strip()
            if not file_path or file_path in seen:
                continue
            seen.add(file_path)
            normalized_paths.append(file_path)

        if not normalized_paths:
            raise ValueError("At least one file_path is required.")
        if len(normalized_paths) > max_files:
            return ToolExecutionPayload(
                content=f"Too many files requested ({len(normalized_paths)}). Limit is {max_files}.",
                output_payload={
                    "files_requested": normalized_paths,
                    "max_files": max_files,
                    "too_many_files": True,
                },
                metadata={"success": False},
                is_error=True,
            )

        rendered_results: list[str] = []
        failures: list[str] = []
        for index, file_path in enumerate(normalized_paths, start=1):
            ok, text, _metadata, error = await self._read_one(
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                max_lines=max_lines,
            )
            if not ok:
                failures.append(f"{file_path}: {error}")
                continue
            rendered_results.append(f"[{index}/{len(normalized_paths)}]\n{text}")

        if failures:
            return ToolExecutionPayload(
                content="Failed to read one or more files: " + " | ".join(failures),
                output_payload={
                    "files_requested": normalized_paths,
                    "failures": failures,
                    "success": False,
                },
                metadata={"files_requested": normalized_paths, "failures": failures, "success": False},
                is_error=True,
            )

        return ToolExecutionPayload(
            content="Batch file reads:\n\n" + "\n\n".join(rendered_results),
            output_payload={
                "files_read": normalized_paths,
                "files_requested": normalized_paths,
                "total_files": len(normalized_paths),
                "success": True,
            },
            metadata={
                "files_read": normalized_paths,
                "files_requested": normalized_paths,
                "total_files": len(normalized_paths),
                "success": True,
            },
        )


class GlobRuntimeTool(_FileScopeMixin, RuntimeTool):
    """Glob: 按文件名/路径枚举(原 ListFilesTool 的 RuntimeTool 直实现)。"""

    name = "Glob"
    description = (
        "按文件名或路径模式枚举项目文件。适合在不知道准确路径时发现路由文件、控制器、服务、配置、"
        "测试、模板、迁移脚本、语言入口文件和特定扩展名文件。\n\n"
        "用法：\n"
        "- path 是相对项目根目录的搜索目录，默认 \".\"。\n"
        "- pattern 是 glob 模式，例如 \"**/*.py\"、\"src/**/*.java\"、\"**/*Controller*\"、\"**/*.xml\"。\n"
        "- recursive 控制是否递归子目录，默认递归。\n"
        "- max_results 控制最多返回的文件数量，避免结果过大。\n\n"
        "使用建议：\n"
        "- 需要找文件名、扩展名、目录结构时使用 Glob。\n"
        "- 找到候选文件后，用 Read 阅读内容。\n"
        "- 需要按内容查找时使用 Grep，不要用 Glob 代替内容搜索。\n"
        "- 如果一次 Glob 返回太多结果，缩小 path 或 pattern。\n\n"
        "审计要求：\n"
        "- 当你需要继续发现相关文件、扩大审计范围或定位未知文件路径时，必须调用 Glob、Grep、Read "
        "或其它合适工具。\n"
        "- 不要只说明“接下来查找相关文件”，必须实际调用工具。"
    )
    input_model = GlobToolInput

    def __init__(
        self,
        *,
        project_root: str,
        exclude_patterns: list[str] | None = None,
        target_files: list[str] | None = None,
        additional_roots: list[str] | None = None,
    ):
        self._init_file_scope(
            project_root=project_root,
            exclude_patterns=exclude_patterns,
            target_files=target_files,
            additional_roots=additional_roots,
        )

    def validate_input(self, raw_input: dict[str, Any]) -> GlobToolInput:
        payload = dict(raw_input or {})
        requested_max_results = _coerce_positive_int(
            payload.get("max_results") or payload.get("max_files"),
            GLOB_DEFAULT_MAX_RESULTS,
        )
        normalized = {
            "path": payload.get("path") or payload.get("directory") or ".",
            "pattern": payload.get("pattern") or payload.get("glob"),
            "recursive": payload.get("recursive", True),
            "max_results": requested_max_results,
            "timeout_seconds": payload.get("timeout_seconds") or RUNTIME_SEARCH_TOOL_TIMEOUT_SECONDS,
        }
        return GlobToolInput.model_validate(normalized)

    def is_concurrency_safe(self, parsed_input: Any = None) -> bool:
        del parsed_input
        return True

    def is_read_only(self, parsed_input: Any = None) -> bool:
        del parsed_input
        return True

    def execution_timeout_seconds(self, parsed_input: Any = None, context: ToolExecutionContext | None = None) -> float | None:
        del context
        return min(RUNTIME_SEARCH_TOOL_MAX_TIMEOUT_SECONDS, max(1, int(parsed_input.timeout_seconds))) + 2

    def _glob_python_fallback(
        self,
        target_dir: str,
        *,
        include_file,
        recursive: bool,
        max_files: int,
        timeout_seconds: int,
    ) -> tuple[list[str], bool, bool]:
        """rg 缺失时的纯 Python 兜底: os.walk/os.scandir + fnmatch 收集文件。"""

        def scan() -> tuple[list[str], bool, bool]:
            deadline = time.monotonic() + timeout_seconds
            found: list[str] = []
            timed_out = False
            truncated = False
            if recursive:
                for root, dirnames, filenames in os.walk(target_dir):
                    dirnames[:] = [d for d in dirnames if d not in self.exclude_dirs]
                    if time.monotonic() > deadline:
                        timed_out = True
                        break
                    for name in sorted(filenames):
                        if time.monotonic() > deadline:
                            timed_out = True
                            break
                        full = os.path.join(root, name)
                        display = _best_display_path(full, self.project_root, self.allowed_roots, full)
                        if include_file(display, name):
                            found.append(display)
                        if len(found) >= max_files:
                            truncated = True
                            break
                    if truncated or timed_out:
                        break
            else:
                try:
                    with os.scandir(target_dir) as it:
                        for entry in it:
                            if time.monotonic() > deadline:
                                timed_out = True
                                break
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            display = _best_display_path(entry.path, self.project_root, self.allowed_roots, entry.path)
                            if include_file(display, entry.name):
                                found.append(display)
                            if len(found) >= max_files:
                                truncated = True
                                break
                except OSError:
                    pass
            return found, timed_out, truncated

        try:
            return scan()
        except TimeoutError:
            return [], True, False

    async def execute(self, parsed_input: GlobToolInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        requested_max_results = parsed_input.max_results
        applied_max_results = min(max(1, int(parsed_input.max_results)), GLOB_HARD_MAX_RESULTS)
        timeout_seconds = min(RUNTIME_SEARCH_TOOL_MAX_TIMEOUT_SECONDS, max(1, int(parsed_input.timeout_seconds)))

        target_dir = _resolve_allowed_path(parsed_input.path, self.allowed_roots)
        if not target_dir:
            return ToolExecutionPayload(
                content="安全错误：不允许访问项目目录外的目录",
                output_payload={"directory": parsed_input.path},
                metadata={"success": False},
                is_error=True,
            )
        if not os.path.exists(target_dir):
            return ToolExecutionPayload(
                content=f"目录不存在: {parsed_input.path}",
                output_payload={"directory": parsed_input.path},
                metadata={"success": False},
                is_error=True,
            )
        if not os.path.isdir(target_dir):
            return ToolExecutionPayload(
                content=f"不是目录: {parsed_input.path}",
                output_payload={"directory": parsed_input.path},
                metadata={"success": False},
                is_error=True,
            )

        def include_file(display_path: str, file_name: str) -> bool:
            if parsed_input.pattern and not fnmatch.fnmatch(file_name, parsed_input.pattern):
                return False
            if self.target_files and display_path not in self.target_files and not display_path.startswith("skill_library/"):
                return False
            if any(fnmatch.fnmatch(display_path, item) or fnmatch.fnmatch(file_name, item) for item in self.exclude_patterns):
                return False
            return True

        timed_out = False
        truncated = False
        files: list[str] = []
        rg = shutil.which("rg")
        if not rg:
            files, timed_out, truncated = await asyncio.to_thread(
                self._glob_python_fallback,
                target_dir,
                include_file=include_file,
                recursive=parsed_input.recursive,
                max_files=applied_max_results,
                timeout_seconds=timeout_seconds,
            )
        else:
            args = [rg, "--files", "--no-messages"]
            if not parsed_input.recursive:
                args.extend(["--max-depth", "1"])
            if parsed_input.pattern:
                args.extend(["--glob", parsed_input.pattern])
            args.extend(_rg_exclude_args(self.exclude_dirs, self.exclude_patterns))
            args.extend(["--", "."])
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=target_dir,
            )
            try:
                assert process.stdout is not None
                async with asyncio.timeout(timeout_seconds):
                    while raw_line := await process.stdout.readline():
                        full_path = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                        if full_path and not os.path.isabs(full_path):
                            full_path = os.path.join(target_dir, full_path)
                        display_path = _best_display_path(full_path, self.project_root, self.allowed_roots, full_path)
                        if not include_file(display_path, os.path.basename(full_path)):
                            continue
                        files.append(display_path)
                        if len(files) >= applied_max_results:
                            truncated = True
                            await _terminate_process(process)
                            break
                if process.returncode is None:
                    await process.wait()
            except TimeoutError:
                timed_out = True
                await _terminate_process(process)
            finally:
                if process.returncode is None:
                    await _terminate_process(process)

        output_parts = [f"目录: {parsed_input.path}\n"]
        dirs: list[str] = []
        if dirs:
            output_parts.append("目录:")
            for item in sorted(dirs)[:20]:
                output_parts.append(f"  {item}")
            if len(dirs) > 20:
                output_parts.append(f"  ... 还有 {len(dirs) - 20} 个目录")

        if files:
            output_parts.append(f"\n文件 ({len(files)}):")
            for item in sorted(files):
                output_parts.append(f"  {item}")
        elif self.target_files:
            output_parts.append(f"\n指定的目标文件 ({len(self.target_files)}):")
            for item in sorted(self.target_files)[:20]:
                output_parts.append(f"  {item}")
            if len(self.target_files) > 20:
                output_parts.append(f"  ... 还有 {len(self.target_files) - 20} 个文件")

        if len(files) >= applied_max_results:
            output_parts.append(f"\n... 结果已截断（最大 {applied_max_results} 个文件）")

        payload = ToolExecutionPayload(
            content="\n".join(output_parts),
            output_payload={
                "directory": parsed_input.path,
                "file_count": len(files),
                "dir_count": len(dirs),
                "truncated": truncated,
                "timed_out": timed_out,
                "timeout_seconds": timeout_seconds,
                "success": True,
            },
            metadata={
                "directory": parsed_input.path,
                "file_count": len(files),
                "dir_count": len(dirs),
                "truncated": truncated,
                "timed_out": timed_out,
                "timeout_seconds": timeout_seconds,
                "success": True,
            },
        )
        return _append_truncation_hint(payload, requested=requested_max_results, limit=applied_max_results)


class GrepRuntimeTool(_FileScopeMixin, RuntimeTool):
    """Grep: 关键字/正则内容搜索(原 FileSearchTool 的 RuntimeTool 直实现)。"""

    name = "Grep"
    description = (
        "在项目代码和配置文本中搜索关键字或正则表达式。底层语义等价于高效代码搜索，适合追踪路由、"
        "函数名、参数名、权限校验、危险 API、source、sink、配置项和跨文件调用关系。\n\n"
        "用法：\n"
        "- pattern 是要搜索的关键字或正则表达式。\n"
        "- path 可选，用于限制搜索目录；不提供时默认从项目根目录搜索。\n"
        "- glob 可选，用于限制文件类型或路径范围，例如 \"*.py\"、\"**/*.java\"、\"src/**/*.ts\"。\n"
        "- case_sensitive 控制是否大小写敏感，默认不敏感。\n"
        "- is_regex=true 时 pattern 按正则处理；普通关键字搜索保持 is_regex=false。\n"
        "- max_results 控制最多返回的匹配数量，避免一次搜索结果过大。\n\n"
        "使用建议：\n"
        "- 搜索任务优先使用 Grep，不要通过 PowerShell 手写 grep/rg/findstr，除非 Grep 无法表达该查询。\n"
        "- 已知标识符、接口路径、参数名、函数名、类名、配置 key 时，先用 Grep 定位引用，再用 Read 阅读关键文件。\n"
        "- 追踪漏洞链时，用 Grep 查找 source 输入点、sink 调用点、鉴权/权限判断、过滤/转义函数、跨层 service/model 调用。\n\n"
        "审计要求：\n"
        "- 当你需要继续搜索、追踪、确认引用、查找调用链或补齐证据时，必须调用 Grep、Read、Glob 或其它合适工具。\n"
        "- 不要只回复“我将继续搜索/继续追踪/下一步检查”，继续就必须实际发起工具调用。"
    )
    input_model = GrepToolInput

    def __init__(
        self,
        *,
        project_root: str,
        exclude_patterns: list[str] | None = None,
        target_files: list[str] | None = None,
        additional_roots: list[str] | None = None,
    ):
        self._init_file_scope(
            project_root=project_root,
            exclude_patterns=exclude_patterns,
            target_files=target_files,
            additional_roots=additional_roots,
        )

    def validate_input(self, raw_input: dict[str, Any]) -> GrepToolInput:
        payload = dict(raw_input or {})
        requested_max_results = _coerce_positive_int(
            payload.get("max_results") or payload.get("limit"),
            GREP_DEFAULT_MAX_RESULTS,
        )
        normalized = {
            "pattern": payload.get("pattern") or payload.get("query") or payload.get("keyword"),
            "path": payload.get("path") or payload.get("directory"),
            "glob": payload.get("glob") or payload.get("file_pattern"),
            "case_sensitive": payload.get("case_sensitive", False),
            "max_results": requested_max_results,
            "is_regex": payload.get("is_regex", False),
            "timeout_seconds": payload.get("timeout_seconds") or RUNTIME_SEARCH_TOOL_TIMEOUT_SECONDS,
        }
        return GrepToolInput.model_validate(normalized)

    def is_concurrency_safe(self, parsed_input: Any = None) -> bool:
        del parsed_input
        return True

    def is_read_only(self, parsed_input: Any = None) -> bool:
        del parsed_input
        return True

    def execution_timeout_seconds(self, parsed_input: Any = None, context: ToolExecutionContext | None = None) -> float | None:
        del context
        return min(RUNTIME_SEARCH_TOOL_MAX_TIMEOUT_SECONDS, max(1, int(parsed_input.timeout_seconds))) + 2

    @staticmethod
    def _normalize_keyword_input(keyword: str | None = None, **kwargs) -> str | None:
        if isinstance(keyword, str) and keyword.strip():
            return keyword.strip()

        for alias in ("query", "pattern", "term", "text", "needle", "raw_input"):
            candidate = kwargs.get(alias)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    @staticmethod
    def _read_file_lines_sync(file_path: str) -> list[str]:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.readlines()

    async def _grep_python_fallback(
        self,
        search_dir: str,
        keyword: str,
        *,
        max_results: int,
        timeout_seconds: int,
        case_sensitive: bool,
        is_regex: bool,
        file_pattern: str | None,
    ) -> tuple[list[dict[str, Any]], bool, bool]:
        """rg 缺失时的纯 Python 兜底: os.walk + 逐行匹配。"""

        def scan() -> tuple[list[dict[str, Any]], bool, bool]:
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                matcher = re.compile(keyword if is_regex else re.escape(keyword), flags).search
            except re.error:
                matcher = re.compile(re.escape(keyword), flags).search
            deadline = time.monotonic() + timeout_seconds
            found: list[dict[str, Any]] = []
            timed_out = False
            truncated = False
            for root, dirnames, filenames in os.walk(search_dir):
                dirnames[:] = [d for d in dirnames if d not in self.exclude_dirs]
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                for name in sorted(filenames):
                    if time.monotonic() > deadline:
                        timed_out = True
                        break
                    full = os.path.join(root, name)
                    display = _best_display_path(full, self.project_root, self.allowed_roots, full)
                    if self.target_files and display not in self.target_files and not display.startswith("skill_library/"):
                        continue
                    if any(fnmatch.fnmatch(display, p) or fnmatch.fnmatch(name, p) for p in self.exclude_patterns):
                        continue
                    if file_pattern and not (fnmatch.fnmatch(name, file_pattern) or fnmatch.fnmatch(display, file_pattern)):
                        continue
                    try:
                        with open(full, "r", encoding="utf-8", errors="replace") as fh:
                            for lineno, raw in enumerate(fh, 1):
                                text = raw.rstrip("\r\n").replace("\x00", "\\x00")
                                if matcher(text):
                                    found.append(
                                        {
                                            "file": display,
                                            "line": lineno,
                                            "match": text[:200],
                                            "context": f"> {lineno:4d}| {text}",
                                        }
                                    )
                                    if len(found) >= max_results:
                                        truncated = True
                                        return found, timed_out, truncated
                    except (OSError, UnicodeError):
                        continue
                if truncated or timed_out:
                    break
            return found, timed_out, truncated

        try:
            async with asyncio.timeout(timeout_seconds):
                return await asyncio.to_thread(scan)
        except TimeoutError:
            return [], True, False

    async def _search_directory(
        self,
        search_dir: str,
        keyword: str,
        *,
        max_results: int,
        timeout_seconds: int,
        case_sensitive: bool,
        is_regex: bool,
        file_pattern: str | None,
    ) -> tuple[list[dict[str, Any]], bool, bool]:
        """返回 (results, timed_out, truncated)。rg 在场走 rg, 否则纯 Python 兜底。"""
        rg = shutil.which("rg")
        if not rg:
            return await self._grep_python_fallback(
                search_dir,
                keyword,
                max_results=max_results,
                timeout_seconds=timeout_seconds,
                case_sensitive=case_sensitive,
                is_regex=is_regex,
                file_pattern=file_pattern,
            )

        args = [rg, "--json", "--no-messages", "--line-number", "--with-filename"]
        if not case_sensitive:
            args.append("--ignore-case")
        if not is_regex:
            args.append("--fixed-strings")
        if file_pattern:
            args.extend(["--glob", file_pattern])
        args.extend(_rg_exclude_args(self.exclude_dirs, self.exclude_patterns))
        args.extend(["--", keyword, "."])
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=search_dir,
        )
        results: list[dict[str, Any]] = []
        timed_out = False
        truncated = False
        try:
            assert process.stdout is not None
            async with asyncio.timeout(timeout_seconds):
                while raw_line := await process.stdout.readline():
                    try:
                        event = json.loads(raw_line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if event.get("type") != "match":
                        continue
                    data = event.get("data") or {}
                    raw_path = str((data.get("path") or {}).get("text") or "")
                    if raw_path and not os.path.isabs(raw_path):
                        raw_path = os.path.join(search_dir, raw_path)
                    display_path = _best_display_path(raw_path, self.project_root, self.allowed_roots, raw_path)
                    if self.target_files and display_path not in self.target_files and not display_path.startswith("skill_library/"):
                        continue
                    text = str((data.get("lines") or {}).get("text") or "").rstrip("\r\n").replace("\x00", "\\x00")
                    line_number = int(data.get("line_number") or 0)
                    results.append(
                        {
                            "file": display_path,
                            "line": line_number,
                            "match": text[:200],
                            "context": f"> {line_number:4d}| {text}",
                        }
                    )
                    if len(results) >= max_results:
                        truncated = True
                        await _terminate_process(process)
                        break
            if process.returncode is None:
                await process.wait()
        except TimeoutError:
            timed_out = True
            await _terminate_process(process)
        finally:
            if process.returncode is None:
                await _terminate_process(process)
        return results, timed_out, truncated

    async def execute(self, parsed_input: GrepToolInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        keyword = self._normalize_keyword_input(parsed_input.pattern)
        if not keyword:
            return ToolExecutionPayload(
                content="Missing required search keyword. Provide keyword, query, pattern, term, or text.",
                output_payload={"success": False},
                metadata={"success": False},
                is_error=True,
            )

        requested_max_results = parsed_input.max_results
        applied_max_results = min(max(1, int(parsed_input.max_results)), GREP_HARD_MAX_RESULTS)
        timeout_seconds = min(RUNTIME_SEARCH_TOOL_MAX_TIMEOUT_SECONDS, max(1, int(parsed_input.timeout_seconds)))
        case_sensitive = bool(parsed_input.case_sensitive)
        is_regex = bool(parsed_input.is_regex)
        file_pattern = str(parsed_input.glob or "").strip() or None

        search_dir = self.project_root if not parsed_input.path else _resolve_allowed_path(parsed_input.path, self.allowed_roots)
        if not search_dir:
            return ToolExecutionPayload(
                content="Security error: search is limited to the audit project and approved shared roots.",
                output_payload={"success": False},
                metadata={"success": False},
                is_error=True,
            )
        # WinError 267 兜底: search_dir 必须是已存在目录(LLM 可能传文件路径)
        if not os.path.isdir(search_dir):
            if parsed_input.path and self.project_root and os.path.isdir(self.project_root):
                search_dir = self.project_root
            else:
                results, timed_out, truncated = await self._grep_python_fallback(
                    search_dir,
                    keyword,
                    max_results=applied_max_results,
                    timeout_seconds=timeout_seconds,
                    case_sensitive=case_sensitive,
                    is_regex=is_regex,
                    file_pattern=file_pattern,
                )
                payload = self._build_grep_payload(
                    keyword=keyword,
                    results=results,
                    timed_out=timed_out,
                    truncated=truncated,
                    timeout_seconds=timeout_seconds,
                    applied_max_results=applied_max_results,
                )
                return _append_truncation_hint(payload, requested=requested_max_results, limit=applied_max_results)

        results, timed_out, truncated = await self._search_directory(
            search_dir,
            keyword,
            max_results=applied_max_results,
            timeout_seconds=timeout_seconds,
            case_sensitive=case_sensitive,
            is_regex=is_regex,
            file_pattern=file_pattern,
        )
        payload = self._build_grep_payload(
            keyword=keyword,
            results=results,
            timed_out=timed_out,
            truncated=truncated,
            timeout_seconds=timeout_seconds,
            applied_max_results=applied_max_results,
        )
        return _append_truncation_hint(payload, requested=requested_max_results, limit=applied_max_results)

    def _build_grep_payload(
        self,
        *,
        keyword: str,
        results: list[dict[str, Any]],
        timed_out: bool,
        truncated: bool,
        timeout_seconds: int,
        applied_max_results: int,
    ) -> ToolExecutionPayload:
        files_searched = len({item["file"] for item in results})

        if not results:
            return ToolExecutionPayload(
                content=f"未找到 '{keyword}' 的匹配结果。\n已搜索 {files_searched} 个文件。",
                output_payload={
                    "files_searched": files_searched,
                    "matches": 0,
                    "keyword": keyword,
                    "timed_out": timed_out,
                    "timeout_seconds": timeout_seconds,
                    "success": True,
                },
                metadata={
                    "files_searched": files_searched,
                    "matches": 0,
                    "keyword": keyword,
                    "timed_out": timed_out,
                    "timeout_seconds": timeout_seconds,
                    "success": True,
                },
            )

        output_parts = [
            f"'{keyword}' 的搜索结果\n",
            f"在 {files_searched} 个文件中找到 {len(results)} 处匹配。\n",
        ]
        for result in results:
            output_parts.append(f"\nFile {result['file']}:{result['line']}")
            output_parts.append(f"```\n{result['context']}\n```")
        if truncated:
            output_parts.append(f"\n... results truncated (max {applied_max_results})")
        if timed_out:
            output_parts.append(f"\n... search stopped after {timeout_seconds}s; complete matches collected so far are shown")

        return ToolExecutionPayload(
            content="\n".join(output_parts),
            output_payload={
                "keyword": keyword,
                "files_searched": files_searched,
                "matches": len(results),
                "results": results[:10],
                "truncated": truncated,
                "timed_out": timed_out,
                "timeout_seconds": timeout_seconds,
                "success": True,
            },
            metadata={
                "keyword": keyword,
                "files_searched": files_searched,
                "matches": len(results),
                "results": results[:10],
                "truncated": truncated,
                "timed_out": timed_out,
                "timeout_seconds": timeout_seconds,
                "success": True,
            },
        )
