"""Unique top-level Java type imports; no build or classpath execution."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from pathlib import PurePosixPath

from app.domains.deep_review.services.blast_analyzers.common import bounded_entries, iter_source_texts
from app.domains.deep_review.services.blast_analyzers.lexing import mask_comments_and_literals


_PACKAGE = re.compile(r"(?m)^[ \t]*package[ \t]+([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)[ \t]*;")
_IMPORT = re.compile(r"(?m)^[ \t]*import[ \t]+(static[ \t]+)?([A-Za-z_$][\w$]*(?:\.[A-Za-z_$*][\w$*]*)+)[ \t]*;")
_TYPE = re.compile(r"\b(?:@interface|class|interface|enum|record)\s+([A-Za-z_$][\w$]*)")


def _top_level_names(source: str) -> set[str]:
    names: set[str] = set()
    cursor = 0
    depth = 0
    for match in _TYPE.finditer(source):
        for char in source[cursor:match.start()]:
            if char == "{":
                depth += 1
            elif char == "}":
                depth = max(0, depth - 1)
        cursor = match.end()
        if depth == 0 and (match.start() == 0 or source[match.start() - 1] != "."):
            names.add(match.group(1))
    return names


def _parse_java(path: str, content: str) -> tuple[str | None, set[str]]:
    if PurePosixPath(path).name in {"module-info.java", "package-info.java"}:
        return None, set()
    source = mask_comments_and_literals(content, mask_quoted=True)
    package_match = _PACKAGE.search(source)
    package = package_match.group(1) if package_match else None
    stem = PurePosixPath(path).stem
    fqcn = f"{package}.{stem}" if package and stem in _top_level_names(source) else None
    imports: set[str] = set()
    for match in _IMPORT.finditer(source):
        name = match.group(2)
        if match.group(1):
            owner = name.rsplit(".", 1)[0]
            if owner:
                imports.add(owner)
        elif not name.endswith(".*"):
            imports.add(name)
    return fqcn, imports


class JavaBlastAnalyzer:
    languages = frozenset({"java"})

    async def analyze(self, context, sink) -> None:
        entries = bounded_entries(context, frozenset({".java"}), "java")
        parsed: dict[str, tuple[str | None, set[str]]] = {}
        paths_by_type: dict[str, set[str]] = defaultdict(set)
        async for path, content in iter_source_texts(context, entries, "java"):
            fqcn, imports = await asyncio.wait_for(
                asyncio.to_thread(_parse_java, path, content), timeout=context.remaining(),
            )
            parsed[path] = (fqcn, imports)
            if fqcn:
                paths_by_type[fqcn].add(path)
        unique_types = {
            name: next(iter(paths)) for name, paths in paths_by_type.items() if len(paths) == 1
        }
        if len(unique_types) < len(paths_by_type):
            context.degrade("java_duplicate_fqcn")
        unresolved_seeds = [seed for seed in context.seed_paths if not parsed.get(seed, (None, set()))[0]]
        if unresolved_seeds:
            context.degrade("java_seed_type_unresolved")
        for path, (_fqcn, imports) in parsed.items():
            if path in context.seed_paths:
                continue
            for imported in imports:
                target = unique_types.get(imported)
                if target in context.seed_paths:
                    sink.add(path=path, seed_path=target, relation="static_import")
