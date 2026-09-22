from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import PurePosixPath

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.services.directory_filter import (
    DirectoryFilter,
    FilterError,
    normalize_path,
)
from app.domains.deep_review.services.git_runner import run_git_output_bounded


class BlastRadiusError(RuntimeError):
    pass


async def head_python_paths(repo_path: str, head_commit: str, config: DeepReviewConfig) -> list[str]:
    result = await run_git_output_bounded(
        repo_path,
        ["ls-tree", "-r", "--name-only", "-z", head_commit],
        max_bytes=config.max_tool_output_bytes,
        timeout_seconds=config.tool_timeout_seconds,
    )
    if result.returncode != 0:
        raise BlastRadiusError("could not read the fixed head tree")
    if result.truncated:
        raise BlastRadiusError("head tree listing exceeded max_tool_output_bytes")
    secret_filter = DirectoryFilter(config)
    paths: list[str] = []
    for path in result.stdout.split("\0"):
        if not path.endswith(".py"):
            continue
        try:
            normalized = normalize_path(path)
        except FilterError:
            continue
        if not secret_filter.is_secret_path(normalized):
            paths.append(normalized)
    return sorted(set(paths))


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
) -> dict[str, list[str]]:
    paths = python_paths if python_paths is not None else await head_python_paths(repo_path, head_commit, config)
    module_map = {_module_name(path): path for path in paths}
    graph: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        content = await read_head_file(repo_path, head_commit, path, config)
        if not content:
            continue
        for module in _import_candidates(content, path):
            target = module_map.get(module)
            if target and target != path:
                graph[path].append(target)
    return dict(graph)


async def compute_blast_radius(
    changed_paths: list[str],
    repo_path: str,
    head_commit: str,
    config: DeepReviewConfig,
    *,
    import_graph: dict[str, list[str]] | None = None,
) -> list[str]:
    graph = import_graph or await build_import_graph(repo_path, head_commit, config)
    changed = set(changed_paths)
    return sorted({
        dependent
        for dependent, imports in graph.items()
        if dependent not in changed and any(target in changed for target in imports)
    })
