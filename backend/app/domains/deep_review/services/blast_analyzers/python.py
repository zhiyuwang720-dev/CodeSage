"""Conservative one-hop Python import scanner; no full dependency graph."""

from __future__ import annotations

import asyncio
import ast
from collections import defaultdict
from pathlib import PurePosixPath

from app.domains.deep_review.services.blast_analyzers.common import bounded_entries, iter_source_texts


def _module_aliases(path: str) -> set[str]:
    parts = list(PurePosixPath(path).parts)
    if parts[-1] in {"__init__.py", "__init__.pyi"}:
        parts = parts[:-1]
    else:
        parts[-1] = PurePosixPath(parts[-1]).stem
    if not parts:
        return set()
    return {".".join(parts[index:]) for index in range(len(parts))}


def _import_candidates(content: str, path: str) -> set[str] | None:
    try:
        tree = ast.parse(content, filename=path)
    except (SyntaxError, ValueError):
        return None
    parent = PurePosixPath(path).parent
    package_parts = list(parent.parts) if str(parent) != "." else []
    candidates: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            candidates.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                levels = max(0, node.level - 1)
                if levels > len(package_parts):
                    continue
                base_parts = package_parts[:len(package_parts) - levels]
                module_parts = node.module.split(".") if node.module else []
                root = ".".join(base_parts + module_parts)
            else:
                root = node.module or ""
            candidates.add(root)
            for alias in node.names:
                candidates.add(f"{root}.{alias.name}" if root else alias.name)
    return {value.strip(".") for value in candidates if value.strip(".")}


class PythonBlastAnalyzer:
    languages = frozenset({"python"})

    async def analyze(self, context, sink) -> None:
        entries = bounded_entries(context, frozenset({".py", ".pyi"}), "python")
        aliases: dict[str, set[str]] = defaultdict(set)
        for entry in entries:
            for alias in _module_aliases(entry.path):
                aliases[alias].add(entry.path)
        unique_aliases = {
            alias: next(iter(paths)) for alias, paths in aliases.items() if len(paths) == 1
        }
        async for path, content in iter_source_texts(context, entries, "python"):
            if path in context.seed_paths:
                continue
            imports = await asyncio.wait_for(
                asyncio.to_thread(_import_candidates, content, path),
                timeout=context.remaining(),
            )
            if imports is None:
                context.degrade("python_syntax_unparsed")
                continue
            for imported in imports:
                target = unique_aliases.get(imported)
                if target in context.seed_paths:
                    sink.add(path=path, seed_path=target, relation="static_import")
