"""Conservative one-hop Python import scanner; no full dependency graph."""

from __future__ import annotations

import asyncio
import ast
from collections import defaultdict
from pathlib import PurePosixPath

from app.domains.deep_review.services.git_runner import run_git_binary_bounded


def _module_aliases(path: str) -> set[str]:
    parts = list(PurePosixPath(path).parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
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


async def _blob_sizes(context, entries) -> dict[str, int]:
    oids = sorted({entry.oid for entry in entries})
    request = "".join(f"{oid}\n" for oid in oids).encode("ascii")
    result = await run_git_binary_bounded(
        context.repo_path, ["cat-file", "--batch-check"],
        input_bytes=request,
        max_bytes=max(4096, len(oids) * 100),
        timeout_seconds=context.remaining(),
    )
    if result.returncode != 0 or result.stdout_truncated or result.stderr_truncated:
        raise RuntimeError("python blob-size query failed or exceeded budget")
    sizes: dict[str, int] = {}
    for line in result.stdout.splitlines():
        fields = line.split(b" ")
        if len(fields) != 3 or fields[1] != b"blob":
            raise RuntimeError("unexpected python blob-size response")
        sizes[fields[0].decode("ascii")] = int(fields[2])
    if set(sizes) != set(oids):
        raise RuntimeError("incomplete python blob-size response")
    return sizes


async def _read_batch(context, batch: list[tuple[str, int]]) -> dict[str, bytes]:
    request = "".join(f"{oid}\n" for oid, _ in batch).encode("ascii")
    expected = sum(size for _, size in batch)
    result = await run_git_binary_bounded(
        context.repo_path, ["cat-file", "--batch"],
        input_bytes=request,
        max_bytes=expected + len(batch) * 128 + 1024,
        timeout_seconds=context.remaining(),
    )
    if result.returncode != 0 or result.stdout_truncated or result.stderr_truncated:
        raise RuntimeError("python source batch read failed or exceeded budget")
    blobs: dict[str, bytes] = {}
    cursor = 0
    for expected_oid, expected_size in batch:
        header_end = result.stdout.find(b"\n", cursor)
        if header_end < 0:
            raise RuntimeError("truncated python source header")
        fields = result.stdout[cursor:header_end].split(b" ")
        if len(fields) != 3 or fields[1] != b"blob":
            raise RuntimeError("unexpected python source header")
        oid = fields[0].decode("ascii")
        size = int(fields[2])
        start = header_end + 1
        end = start + size
        if oid != expected_oid or size != expected_size or end >= len(result.stdout) or result.stdout[end] != 10:
            raise RuntimeError("inconsistent python source batch")
        blobs[oid] = result.stdout[start:end]
        cursor = end + 1
    return blobs


class PythonBlastAnalyzer:
    languages = frozenset({"python"})

    async def analyze(self, context, sink) -> None:
        entries = context.head_tree.source_entries(frozenset({".py"}))
        if len(entries) > context.config.max_import_graph_files:
            entries = entries[:context.config.max_import_graph_files]
            context.degrade("python_file_count_limited")
        aliases: dict[str, set[str]] = defaultdict(set)
        for entry in entries:
            for alias in _module_aliases(entry.path):
                aliases[alias].add(entry.path)
        unique_aliases = {alias: next(iter(paths)) for alias, paths in aliases.items() if len(paths) == 1}
        sizes = await _blob_sizes(context, entries) if entries else {}
        paths_by_oid: dict[str, list[str]] = defaultdict(list)
        for entry in entries:
            paths_by_oid[entry.oid].append(entry.path)
        selected: list[tuple[str, int]] = []
        remaining_bytes = context.config.max_import_scan_bytes
        for oid in dict.fromkeys(entry.oid for entry in entries):
            size = sizes[oid]
            if size > context.config.max_blast_source_file_bytes:
                context.degrade("python_source_too_large")
                continue
            if size > remaining_bytes:
                context.degrade("python_scan_bytes_limited")
                continue
            selected.append((oid, size))
            remaining_bytes -= size
        for offset in range(0, len(selected), 64):
            batch: list[tuple[str, int]] = []
            batch_bytes = 0
            for oid, size in selected[offset:offset + 64]:
                if batch and batch_bytes + size > context.config.max_blast_source_file_bytes:
                    await self._scan_batch(context, sink, batch, paths_by_oid, unique_aliases)
                    batch = []
                    batch_bytes = 0
                batch.append((oid, size))
                batch_bytes += size
            if batch:
                await self._scan_batch(context, sink, batch, paths_by_oid, unique_aliases)

    async def _scan_batch(self, context, sink, batch, paths_by_oid, unique_aliases) -> None:
        blobs = await _read_batch(context, batch)
        for oid, size in batch:
            context.remaining()
            raw = blobs[oid]
            if b"\0" in raw:
                context.degrade("python_binary_source_skipped")
                continue
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                context.degrade("python_non_utf8_source_skipped")
                continue
            context.scanned_bytes += size
            for path in paths_by_oid[oid]:
                context.scanned_file_count += 1
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
