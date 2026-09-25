"""Repository-relative quote includes only; no build-system include search."""

from __future__ import annotations

import asyncio
import posixpath
import re
from pathlib import PurePosixPath

from app.domains.deep_review.services.blast_analyzers.common import bounded_entries, iter_source_texts
from app.domains.deep_review.services.blast_analyzers.lexing import mask_comments_and_literals


SUFFIXES = frozenset({".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx"})
_QUOTE_INCLUDE = re.compile(r'(?m)^\s*#\s*include\s*"([^"\n]+)"')


def _includes(content: str) -> set[str]:
    return set(_QUOTE_INCLUDE.findall(mask_comments_and_literals(content)))


def _resolve_quote(importer: str, include: str, paths: set[str]) -> str | None:
    candidate = posixpath.normpath(posixpath.join(PurePosixPath(importer).parent.as_posix(), include))
    if candidate.startswith("../") or candidate in {"..", "."} or candidate.startswith("/"):
        return None
    return candidate if candidate in paths else None


class CFamilyBlastAnalyzer:
    languages = frozenset({"c", "cpp", "c_family"})

    async def analyze(self, context, sink) -> None:
        entries = bounded_entries(context, SUFFIXES, "c_family")
        paths = {entry.path for entry in entries}
        async for path, content in iter_source_texts(context, entries, "c_family"):
            if path in context.seed_paths:
                continue
            includes = await asyncio.wait_for(
                asyncio.to_thread(_includes, content), timeout=context.remaining(),
            )
            for include in includes:
                target = _resolve_quote(path, include, paths)
                if target in context.seed_paths:
                    sink.add(path=path, seed_path=target, relation="header_include")
