"""Bounded fixed-head source reading shared by small language adapters."""

from __future__ import annotations

from collections import defaultdict
from typing import AsyncIterator

from app.domains.deep_review.services.git_runner import run_git_binary_bounded
from app.domains.deep_review.services.head_tree import HeadEntry


async def _blob_sizes(context, entries: list[HeadEntry]) -> dict[str, int]:
    oids = sorted({entry.oid for entry in entries})
    request = "".join(f"{oid}\n" for oid in oids).encode("ascii")
    result = await run_git_binary_bounded(
        context.repo_path, ["cat-file", "--batch-check"],
        input_bytes=request,
        max_bytes=max(4096, len(oids) * 100),
        timeout_seconds=context.remaining(),
    )
    if result.returncode != 0 or result.stdout_truncated or result.stderr_truncated:
        raise RuntimeError("source blob-size query failed or exceeded budget")
    sizes: dict[str, int] = {}
    for line in result.stdout.splitlines():
        fields = line.split(b" ")
        if len(fields) != 3 or fields[1] != b"blob":
            raise RuntimeError("unexpected source blob-size response")
        sizes[fields[0].decode("ascii")] = int(fields[2])
    if set(sizes) != set(oids):
        raise RuntimeError("incomplete source blob-size response")
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
        raise RuntimeError("source batch read failed or exceeded budget")
    blobs: dict[str, bytes] = {}
    cursor = 0
    for expected_oid, expected_size in batch:
        header_end = result.stdout.find(b"\n", cursor)
        if header_end < 0:
            raise RuntimeError("truncated source batch header")
        fields = result.stdout[cursor:header_end].split(b" ")
        if len(fields) != 3 or fields[1] != b"blob":
            raise RuntimeError("unexpected source batch header")
        oid = fields[0].decode("ascii")
        size = int(fields[2])
        start = header_end + 1
        end = start + size
        if oid != expected_oid or size != expected_size or end >= len(result.stdout) or result.stdout[end] != 10:
            raise RuntimeError("inconsistent source batch")
        blobs[oid] = result.stdout[start:end]
        cursor = end + 1
    return blobs


def bounded_entries(context, suffixes: frozenset[str], label: str) -> list[HeadEntry]:
    entries = context.head_tree.source_entries(suffixes)
    if len(entries) > context.config.max_import_graph_files:
        entries = entries[:context.config.max_import_graph_files]
        context.degrade(f"{label}_file_count_limited")
    return entries


async def iter_source_texts(context, entries: list[HeadEntry], label: str) -> AsyncIterator[tuple[str, str]]:
    if not entries:
        return
    sizes = await _blob_sizes(context, entries)
    paths_by_oid: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        paths_by_oid[entry.oid].append(entry.path)
    selected: list[tuple[str, int]] = []
    remaining_bytes = context.scan_byte_budget
    for oid in dict.fromkeys(entry.oid for entry in entries):
        size = sizes[oid]
        if size > context.config.max_blast_source_file_bytes:
            context.degrade(f"{label}_source_too_large")
            continue
        if size > remaining_bytes:
            context.degrade(f"{label}_scan_bytes_limited")
            continue
        selected.append((oid, size))
        remaining_bytes -= size
    for offset in range(0, len(selected), 64):
        batch: list[tuple[str, int]] = []
        batch_bytes = 0
        for oid, size in selected[offset:offset + 64]:
            if batch and batch_bytes + size > context.config.max_blast_source_file_bytes:
                async for item in _yield_batch(context, batch, paths_by_oid, label):
                    yield item
                batch = []
                batch_bytes = 0
            batch.append((oid, size))
            batch_bytes += size
        if batch:
            async for item in _yield_batch(context, batch, paths_by_oid, label):
                yield item


async def _yield_batch(context, batch, paths_by_oid, label) -> AsyncIterator[tuple[str, str]]:
    blobs = await _read_batch(context, batch)
    for oid, size in batch:
        context.remaining()
        raw = blobs[oid]
        if b"\0" in raw:
            context.degrade(f"{label}_binary_source_skipped")
            continue
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            context.degrade(f"{label}_non_utf8_source_skipped")
            continue
        context.scanned_bytes += size
        for path in paths_by_oid[oid]:
            context.scanned_file_count += 1
            yield path, content
