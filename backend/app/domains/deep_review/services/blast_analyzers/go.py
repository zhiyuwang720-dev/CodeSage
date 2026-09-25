"""Conservative Go package and module-import relationships."""

from __future__ import annotations

import asyncio
import re
from pathlib import PurePosixPath

from app.domains.deep_review.services.blast_analyzers.common import bounded_entries, iter_source_texts
from app.domains.deep_review.services.blast_analyzers.lexing import mask_comments_and_literals


_PACKAGE = re.compile(r"(?m)^\s*package\s+([A-Za-z_]\w*)\b")
_MODULE = re.compile(r"(?m)^\s*module\s+(\S+)")
_SINGLE_IMPORT = re.compile(r'(?m)^\s*import\s+"([^"\n]+)"')
_IMPORT_BLOCK = re.compile(r"(?ms)^\s*import\s*\((.*?)^\s*\)")
_BLOCK_ITEM = re.compile(r'(?m)^\s*(?:(?:[A-Za-z_]\w*|\.|_)\s+)?"([^"\n]+)"')


def _parse_go(content: str) -> tuple[str | None, set[str]]:
    source = mask_comments_and_literals(content)
    package_match = _PACKAGE.search(source)
    imports = set(_SINGLE_IMPORT.findall(source))
    for block in _IMPORT_BLOCK.findall(source):
        imports.update(_BLOCK_ITEM.findall(block))
    return (package_match.group(1) if package_match else None), imports


class GoBlastAnalyzer:
    languages = frozenset({"go"})

    async def analyze(self, context, sink) -> None:
        module_entries = [
            entry for entry in context.head_tree.entries.values()
            if PurePosixPath(entry.path).name == "go.mod"
        ]
        module_path: str | None = None
        module_root = PurePosixPath(".")
        if len(module_entries) == 1:
            module_entry = module_entries[0]
            async for _, content in iter_source_texts(context, [module_entry], "go"):
                match = _MODULE.search(mask_comments_and_literals(content))
                if match and not re.search(r"(?m)^\s*replace\b", content):
                    module_path = match.group(1).strip('"')
                    module_root = PurePosixPath(module_entry.path).parent
        if module_path is None:
            context.degrade("go_module_unavailable_or_ambiguous")

        entries = bounded_entries(context, frozenset({".go"}), "go")
        parsed: dict[str, tuple[str | None, set[str]]] = {}
        async for path, content in iter_source_texts(context, entries, "go"):
            parsed[path] = await asyncio.wait_for(
                asyncio.to_thread(_parse_go, content), timeout=context.remaining(),
            )
        for seed in sorted(context.seed_paths):
            seed_info = parsed.get(seed)
            if seed_info is None or seed_info[0] is None:
                context.degrade("go_seed_package_unparsed")
                continue
            seed_directory = PurePosixPath(seed).parent
            import_path: str | None = None
            if module_path is not None:
                try:
                    relative = seed_directory.relative_to(module_root)
                except ValueError:
                    context.degrade("go_seed_outside_module")
                else:
                    import_path = module_path if str(relative) == "." else f"{module_path}/{relative.as_posix()}"
            for path, (package, imports) in parsed.items():
                if path == seed:
                    continue
                if PurePosixPath(path).parent == seed_directory and package == seed_info[0]:
                    sink.add(path=path, seed_path=seed, relation="same_package")
                elif import_path and import_path in imports:
                    sink.add(path=path, seed_path=seed, relation="static_import")
