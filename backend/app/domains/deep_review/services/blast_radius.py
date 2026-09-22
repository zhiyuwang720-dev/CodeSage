from __future__ import annotations

import ast
from dataclasses import dataclass
from collections import defaultdict
from pathlib import PurePosixPath

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.services.directory_filter import (
    DirectoryFilter,
    FilterError,
    normalize_path,
)
from app.domains.deep_review.services.git_runner import (
    run_git_binary_bounded,
    run_git_output_bounded,
)


class BlastRadiusError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _HeadPythonBlob:
    path: str
    oid: str


async def head_python_paths(repo_path: str, head_commit: str, config: DeepReviewConfig) -> list[str]:
    entries = await _head_python_blobs(repo_path, head_commit, config)
    return sorted(entry.path for entry in entries)


async def _head_python_blobs(
    repo_path: str,
    head_commit: str,
    config: DeepReviewConfig,
) -> list[_HeadPythonBlob]:
    result = await run_git_output_bounded(
        repo_path,
        ["ls-tree", "-r", "-z", head_commit],
        max_bytes=config.max_import_scan_bytes,
        timeout_seconds=config.tool_timeout_seconds,
    )
    if result.returncode != 0:
        raise BlastRadiusError("could not read the fixed head tree")
    if result.truncated:
        raise BlastRadiusError("head tree listing exceeded max_tool_output_bytes")
    secret_filter = DirectoryFilter(config)
    paths: list[str] = []
    for record in result.stdout.split("\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition("\t")
        if not separator:
            raise BlastRadiusError("malformed git ls-tree output")
        fields = metadata.split(" ")
        if len(fields) != 3 or fields[1] != "blob":
            continue
        if not raw_path.endswith(".py"):
            continue
        try:
            normalized = normalize_path(raw_path)
        except FilterError:
            continue
        if not secret_filter.is_secret_path(normalized):
            paths.append(_HeadPythonBlob(path=normalized, oid=fields[2]))
    if len(paths) > config.max_import_graph_files:
        raise BlastRadiusError(
            f"import graph file limit exceeded: {len(paths)} > {config.max_import_graph_files}"
        )
    return sorted(paths, key=lambda item: item.path)


async def read_head_file(repo_path: str, head_commit: str, path: str, config: DeepReviewConfig) -> str:
    try:
        normalized = normalize_path(path)
    except FilterError:
        return ""
    result = await run_git_output_bounded(
        repo_path,
        ["show", f"{head_commit}:{normalized}"],
        max_bytes=config.max_file_bytes,
        timeout_seconds=config.tool_timeout_seconds,
    )
    if result.returncode != 0 or result.truncated or "\x00" in result.stdout:
        return ""
    return result.stdout


def _module_name(path: str) -> str:
    parts = list(PurePosixPath(path).parts)
    if parts and parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def _module_aliases(path: str) -> list[str]:
    module = _module_name(path)
    aliases = {module}
    module_parts = module.split(".")
    aliases.update(".".join(module_parts[index:]) for index in range(1, len(module_parts)))
    return sorted(aliases)


def _import_candidates(content: str, path: str) -> set[str]:
    try:
        tree = ast.parse(content, filename=path)
    except (SyntaxError, ValueError):
        return set()
    parent = PurePosixPath(path).parent
    package_parts = list(parent.parts) if str(parent) != "." else []
    candidates: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            candidates.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            levels = max(0, node.level - 1)
            if levels > len(package_parts):
                continue
            base_parts = package_parts[: len(package_parts) - levels]
            module_parts = node.module.split(".") if node.module else []
            root = ".".join(base_parts + module_parts)
        else:
            root = node.module or ""
        candidates.add(root)
        for alias in node.names:
            candidates.add(f"{root}.{alias.name}" if root else alias.name)
    return {candidate.strip(".") for candidate in candidates if candidate.strip(".")}


async def build_import_graph(
    repo_path: str,
    head_commit: str,
    config: DeepReviewConfig,
    *,
    python_paths: list[str] | None = None,
    diagnostics: list[str] | None = None,
) -> dict[str, list[str]]:
    entries = (
        [_HeadPythonBlob(path=path, oid="") for path in python_paths]
        if python_paths is not None
        else await _head_python_blobs(repo_path, head_commit, config)
    )
    if len(entries) > config.max_import_graph_files:
        raise BlastRadiusError(
            f"import graph file limit exceeded: {len(entries)} > {config.max_import_graph_files}"
        )
    alias_paths: dict[str, set[str]] = defaultdict(set)
    for entry in entries:
        for alias in _module_aliases(entry.path):
            alias_paths[alias].add(entry.path)
    module_map: dict[str, str] = {}
    for alias in sorted(alias_paths):
        candidate_paths = alias_paths[alias]
        if len(candidate_paths) == 1:
            module_map[alias] = next(iter(candidate_paths))
    graph: dict[str, list[str]] = defaultdict(list)
    if entries and python_paths is None:
        contents = await _read_head_blobs(repo_path, head_commit, entries, config, diagnostics)
    else:
        contents = {}
    for entry in entries:
        path = entry.path
        if python_paths is None:
            content = contents.get(path, "")
        else:
            content = await read_head_file(repo_path, head_commit, path, config)
        if not content:
            continue
        for module in _import_candidates(content, path):
            target = module_map.get(module)
            if target and target != path:
                graph[path].append(target)
    return dict(graph)


async def _read_head_blobs(
    repo_path: str,
    head_commit: str,
    entries: list[_HeadPythonBlob],
    config: DeepReviewConfig,
    diagnostics: list[str] | None,
) -> dict[str, str]:
    del head_commit
    if not entries:
        return {}
    oid_by_value: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        if entry.oid:
            oid_by_value[entry.oid].append(entry.path)
    request = "".join(f"{oid}\n" for oid in sorted(oid_by_value)).encode("ascii")
    try:
        result = await run_git_binary_bounded(
            repo_path,
            ["cat-file", "--batch"],
            max_bytes=config.max_import_scan_bytes + 65_536,
            timeout_seconds=config.tool_timeout_seconds,
            input_bytes=request,
        )
    except TimeoutError as exc:
        raise BlastRadiusError("import graph scan timed out") from exc
    except OSError as exc:
        raise BlastRadiusError(f"could not batch read head blobs: {exc}") from exc
    if result.returncode != 0:
        raise BlastRadiusError("git cat-file --batch failed")
    if result.truncated:
        raise BlastRadiusError(
            f"import graph scan limit exceeded: {config.max_import_scan_bytes} bytes"
        )

    contents: dict[str, str] = {}
    skipped = 0
    used = 0
    cursor = 0
    while cursor < len(result.stdout):
        header_end = result.stdout.find(b"\n", cursor)
        if header_end < 0:
            raise BlastRadiusError("truncated git cat-file header")
        header = result.stdout[cursor:header_end].decode("ascii", "replace")
        fields = header.split(" ")
        if len(fields) != 3 or fields[1] != "blob":
            raise BlastRadiusError(f"unexpected git cat-file response: {header[:100]}")
        oid, _, size_text = fields
        try:
            size = int(size_text)
        except ValueError as exc:
            raise BlastRadiusError("invalid git cat-file blob size") from exc
        content_start = header_end + 1
        content_end = content_start + size
        if content_end >= len(result.stdout) or result.stdout[content_end] != 10:
            raise BlastRadiusError("truncated git cat-file blob")
        used += size
        if used > config.max_import_scan_bytes:
            raise BlastRadiusError(
                f"import graph scan limit exceeded: {used} > {config.max_import_scan_bytes}"
            )
        blob = result.stdout[content_start:content_end]
        cursor = content_end + 1
        if size > config.max_file_bytes:
            skipped += 1
            continue
        content = blob.decode("utf-8", "replace")
        if "\x00" in content:
            continue
        for path in oid_by_value.get(oid, []):
            contents[path] = content
    if skipped and diagnostics is not None:
        diagnostics.append(f"import_graph_oversized_files_skipped: {skipped}")
    return contents


async def compute_blast_radius(
    changed_paths: list[str],
    repo_path: str,
    head_commit: str,
    config: DeepReviewConfig,
    *,
    import_graph: dict[str, list[str]] | None = None,
    diagnostics: list[str] | None = None,
) -> list[str]:
    if import_graph is not None:
        graph = import_graph
    else:
        if not changed_paths:
            return []
        graph = await build_import_graph(
            repo_path,
            head_commit,
            config,
            diagnostics=diagnostics,
        )
    changed = set(changed_paths)
    return sorted({
        dependent
        for dependent, imports in graph.items()
        if dependent not in changed and any(target in changed for target in imports)
    })
