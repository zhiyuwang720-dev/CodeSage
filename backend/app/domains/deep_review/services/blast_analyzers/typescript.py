"""Relative static imports for TypeScript and JavaScript source trees."""

from __future__ import annotations

import asyncio
import posixpath
import re
from pathlib import PurePosixPath

from app.domains.deep_review.services.blast_analyzers.common import bounded_entries, iter_source_texts
from app.domains.deep_review.services.blast_analyzers.lexing import mask_comments_and_literals


SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
_FROM = re.compile(r"\bfrom\s*['\"]([^'\"\n]+)['\"]")
_SIDE_EFFECT = re.compile(r"^\s*import\s*['\"]([^'\"\n]+)['\"]")
_REQUIRE = re.compile(
    r"^\s*(?:(?:const|let|var)\s+[^=\n]+|module\.exports)\s*=\s*require\s*\(\s*['\"]([^'\"\n]+)['\"]\s*\)"
)


def _specifiers(content: str) -> set[str]:
    source = mask_comments_and_literals(content)
    lines = source.splitlines()
    found: set[str] = set()
    for index, line in enumerate(lines):
        if re.match(r"^\s*(?:import|export)\b", line):
            match = _SIDE_EFFECT.search(line)
            if match:
                found.add(match.group(1))
                continue
            statement = line
            if "{" in line and "}" not in line:
                for following in lines[index + 1:index + 9]:
                    statement += " " + following
                    if "}" in following or ";" in following:
                        break
            match = _FROM.search(statement)
            if match:
                found.add(match.group(1))
        else:
            match = _REQUIRE.search(line)
            if match:
                found.add(match.group(1))
    return {value for value in found if value.startswith("./") or value.startswith("../")}


def _resolve_relative(importer: str, specifier: str, paths: set[str]) -> str | None:
    parent = PurePosixPath(importer).parent.as_posix()
    base = posixpath.normpath(posixpath.join(parent, specifier))
    if base.startswith("../") or base in {"..", "."} or base.startswith("/"):
        return None
    possibilities = [base]
    if PurePosixPath(base).suffix:
        if base.endswith(".js"):
            possibilities.extend((base[:-3] + ".ts", base[:-3] + ".tsx"))
    else:
        possibilities.extend(base + suffix for suffix in sorted(SUFFIXES))
        possibilities.extend(base + "/index" + suffix for suffix in sorted(SUFFIXES))
    matches = {path for path in possibilities if path in paths}
    return next(iter(matches)) if len(matches) == 1 else None


class TypeScriptBlastAnalyzer:
    languages = frozenset({"typescript", "javascript"})

    async def analyze(self, context, sink) -> None:
        entries = bounded_entries(context, SUFFIXES, "typescript")
        paths = {entry.path for entry in entries}
        async for path, content in iter_source_texts(context, entries, "typescript"):
            if path in context.seed_paths:
                continue
            specs = await asyncio.wait_for(
                asyncio.to_thread(_specifiers, content), timeout=context.remaining(),
            )
            for specifier in specs:
                target = _resolve_relative(path, specifier, paths)
                if target in context.seed_paths:
                    sink.add(path=path, seed_path=target, relation="static_import")
