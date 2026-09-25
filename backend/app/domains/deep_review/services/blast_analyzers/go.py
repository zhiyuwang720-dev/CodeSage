"""Conservative Go package and module-import relationships."""

from __future__ import annotations

import asyncio
import re
from pathlib import PurePosixPath

from app.domains.deep_review.services.blast_analyzers.common import bounded_entries, iter_source_texts
from app.domains.deep_review.services.blast_analyzers.lexing import mask_comments_and_literals


_PACKAGE = re.compile(r"(?m)^[ \t]*package[ \t]+([A-Za-z_]\w*)\b")
_MODULE = re.compile(r"(?m)^[ \t]*module[ \t]+(\S+)")
_SINGLE_IMPORT = re.compile(r'(?m)^[ \t]*import[ \t]+"([^"\n]+)"')
_IMPORT_BLOCK = re.compile(r"(?ms)^[ \t]*import[ \t]*\((.*?)^[ \t]*\)")
_BLOCK_ITEM = re.compile(r'(?m)^[ \t]*(?:(?:[A-Za-z_]\w*|\.|_)[ \t]+)?"([^"\n]+)"')


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
        modules: dict[PurePosixPath, str] = {}
        async for path, content in iter_source_texts(context, module_entries[:32], "go"):
            match = _MODULE.search(mask_comments_and_literals(content))
            if match and not re.search(r"(?m)^[ \t]*replace\b", content):
                modules[PurePosixPath(path).parent] = match.group(1).strip('"')
        if len(module_entries) > 32:
            context.degrade("go_module_count_limited")

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
            matching_roots = [root for root in modules if root == PurePosixPath(".") or root == seed_directory or root in seed_directory.parents]
            if matching_roots:
                module_root = max(matching_roots, key=lambda root: len(root.parts))
                module_path = modules[module_root]
                try:
                    relative = seed_directory.relative_to(module_root)
                except ValueError:
                    context.degrade("go_seed_outside_module")
                else:
                    import_path = module_path if str(relative) == "." else f"{module_path}/{relative.as_posix()}"
            else:
                context.degrade("go_module_unavailable_or_ambiguous")
            for path, (package, imports) in parsed.items():
                if path == seed:
                    continue
                if PurePosixPath(path).parent == seed_directory and package == seed_info[0]:
                    sink.add(path=path, seed_path=seed, relation="same_package")
                elif import_path and import_path in imports:
                    sink.add(path=path, seed_path=seed, relation="static_import")
