from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Protocol

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.services.directory_filter import DirectoryFilter
from app.domains.deep_review.services.head_tree import HeadTreeIndex, HeadTreeUnavailable, load_head_tree_index
from app.domains.deep_review.schemas.input import ChangeType, FileChange
from app.domains.deep_review.services.diff_engine import detect_language


@dataclass(frozen=True, slots=True)
class BlastHint:
    path: str
    seed_path: str
    relation: str


@dataclass(frozen=True, slots=True)
class BlastResult:
    hints: tuple[BlastHint, ...] = ()
    observed_hint_count: int = 0
    coverage_by_language: dict[str, str] = field(default_factory=dict)
    truncated: bool = False
    diagnostics: tuple[str, ...] = ()
    scanned_file_count: int = 0
    scanned_bytes: int = 0

    def prompt_projection(self) -> dict:
        states = sorted(self.coverage_by_language.items())
        projection: dict = {
            "coverage": dict(states[:8]),
            "other_language_count": max(0, len(states) - 8),
            "truncated": self.truncated,
            "hints": [],
        }
        if len(json.dumps(projection, ensure_ascii=False, sort_keys=True).encode("utf-8")) > 1500:
            projection["coverage"] = {}
            projection["coverage_omitted"] = True
            projection["truncated"] = True
        seen_paths: set[str] = set()
        for hint in self.hints:
            if hint.path in seen_paths:
                continue
            if len(projection["hints"]) >= 12:
                projection["truncated"] = True
                break
            item = {"path": hint.path, "relation": hint.relation}
            trial = {**projection, "hints": [*projection["hints"], item]}
            if len(json.dumps(trial, ensure_ascii=False, sort_keys=True).encode("utf-8")) > 1500:
                projection["truncated"] = True
                continue
            projection["hints"].append(item)
            seen_paths.add(hint.path)
        return projection

    @property
    def displayed_paths(self) -> list[str]:
        return [item["path"] for item in self.prompt_projection()["hints"]]


class BlastAnalyzer(Protocol):
    languages: frozenset[str]

    async def analyze(self, context: "BlastContext", sink: "BlastSink") -> None: ...


@dataclass(slots=True)
class BlastContext:
    repo_path: str
    head_commit: str
    seed_paths: frozenset[str]
    head_tree: HeadTreeIndex
    config: DeepReviewConfig
    deadline: float
    scan_byte_budget: int
    diagnostics: list[str] = field(default_factory=list)
    degraded: bool = False
    scanned_file_count: int = 0
    scanned_bytes: int = 0

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Blast Radius global deadline exceeded")
        return min(float(self.config.tool_timeout_seconds), remaining)

    def degrade(self, reason: str) -> None:
        self.degraded = True
        if reason not in self.diagnostics:
            self.diagnostics.append(reason)


class BlastSink:
    """Fixed-size, deterministic retention; raw hit count needs no global set."""

    _BUCKET_COUNT = 8
    _PER_BUCKET = 6
    _RELATION_ORDER = {"static_import": 0, "header_include": 0, "same_package": 1, "test_pair": 2}

    def __init__(self, head_tree: HeadTreeIndex, seed_paths: frozenset[str], config: DeepReviewConfig):
        self.head_tree = head_tree
        self.seed_paths = seed_paths
        self.path_filter = DirectoryFilter(config)
        self.buckets: list[dict[tuple[str, str, str], BlastHint]] = [
            {} for _ in range(self._BUCKET_COUNT)
        ]
        self.observed_hint_count = 0
        self.truncated = False

    def _rank(self, hint: BlastHint) -> tuple:
        return (self._RELATION_ORDER.get(hint.relation, 3), hint.seed_path, hint.path, hint.relation)

    def add(self, *, path: str, seed_path: str, relation: str) -> None:
        if (
            seed_path not in self.seed_paths or path in self.seed_paths
            or self.head_tree.get(path) is None or not self.path_filter.is_related_path_allowed(path)
        ):
            return
        self.observed_hint_count += 1
        hint = BlastHint(path=path, seed_path=seed_path, relation=relation)
        key = (path, seed_path, relation)
        directory = str(PurePosixPath(seed_path).parent)
        bucket_index = hashlib.sha256(directory.encode("utf-8")).digest()[0] % self._BUCKET_COUNT
        bucket = self.buckets[bucket_index]
        if key in bucket:
            return
        if len(bucket) < self._PER_BUCKET:
            bucket[key] = hint
            return
        worst_key = max(bucket, key=lambda value: self._rank(bucket[value]))
        if self._rank(hint) < self._rank(bucket[worst_key]):
            del bucket[worst_key]
            bucket[key] = hint
        self.truncated = True

    def retained(self) -> tuple[BlastHint, ...]:
        groups = [sorted(bucket.values(), key=self._rank) for bucket in self.buckets]
        selected: list[BlastHint] = []
        for offset in range(self._PER_BUCKET):
            for group in groups:
                if offset < len(group):
                    selected.append(group[offset])
        return tuple(selected)


def _language_for_path(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    return detect_language(path) or f"unsupported:{(suffix or '[none]')[:16]}"


def _add_test_pairs(seed_paths: frozenset[str], head_tree: HeadTreeIndex, sink: BlastSink) -> None:
    """Only conventional, same-directory names; a weak investigation hint."""
    for seed in sorted(seed_paths):
        source = PurePosixPath(seed)
        stem, suffix = source.stem, source.suffix.lower()
        if stem.startswith("test_") or stem.endswith(("_test", "Test", ".test", ".spec")):
            continue
        names: tuple[str, ...]
        if suffix in {".py", ".pyi", ".go", ".c", ".cc", ".cpp", ".cxx"}:
            names = (f"test_{stem}{suffix}", f"{stem}_test{suffix}")
        elif suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
            names = (f"{stem}.test{suffix}", f"{stem}.spec{suffix}")
        elif suffix == ".java":
            names = (f"{stem}Test.java",)
        else:
            continue
        for name in names:
            path = (source.parent / name).as_posix()
            if head_tree.get(path) is not None:
                sink.add(path=path, seed_path=seed, relation="test_pair")


async def analyze_blast_radius(
    changed_paths: list[str], repo_path: str, head_commit: str, config: DeepReviewConfig,
    *, changes: list[FileChange] | None = None,
) -> tuple[BlastResult, HeadTreeIndex | None]:
    """Produce bounded hints and honest per-language coverage at fixed head."""
    from app.domains.deep_review.services.blast_analyzers.c_family import CFamilyBlastAnalyzer
    from app.domains.deep_review.services.blast_analyzers.go import GoBlastAnalyzer
    from app.domains.deep_review.services.blast_analyzers.java import JavaBlastAnalyzer
    from app.domains.deep_review.services.blast_analyzers.python import PythonBlastAnalyzer
    from app.domains.deep_review.services.blast_analyzers.typescript import TypeScriptBlastAnalyzer

    coverage = {language: "unsupported" for language in map(_language_for_path, changed_paths)}
    analyzers: list[BlastAnalyzer] = [
        PythonBlastAnalyzer(), GoBlastAnalyzer(), TypeScriptBlastAnalyzer(),
        CFamilyBlastAnalyzer(), JavaBlastAnalyzer(),
    ]
    active = [analyzer for analyzer in analyzers if analyzer.languages.intersection(coverage)]
    diagnostics: list[str] = []
    if changes:
        prior_only = sum(
            item.change_type is ChangeType.DELETED
            or (item.change_type is ChangeType.RENAMED and bool(item.old_path))
            for item in changes
        )
        if prior_only:
            diagnostics.append(f"prior_path_dependencies_not_analyzed:{prior_only}")
    if not active:
        return BlastResult(coverage_by_language=coverage, diagnostics=tuple(diagnostics)), None
    for analyzer in active:
        for language in analyzer.languages.intersection(coverage):
            coverage[language] = "degraded"
    deadline = time.monotonic() + config.max_blast_seconds
    try:
        head_tree = await load_head_tree_index(
            repo_path, head_commit, config,
            timeout_seconds=min(config.tool_timeout_seconds, max(0.001, deadline - time.monotonic())),
        )
    except asyncio.CancelledError:
        raise
    except (HeadTreeUnavailable, OSError, TimeoutError) as exc:
        diagnostics.append(f"head_tree_unavailable:{type(exc).__name__}")
        return BlastResult(coverage_by_language=coverage, truncated=True, diagnostics=tuple(diagnostics)), None
    all_seeds = frozenset(path for path in changed_paths if head_tree.get(path) is not None)
    sink = BlastSink(head_tree, all_seeds, config)
    _add_test_pairs(all_seeds, head_tree, sink)
    scanned_files = 0
    scanned_bytes = 0
    degraded = False
    for index, analyzer in enumerate(active):
        languages = analyzer.languages.intersection(coverage)
        label = sorted(languages)[0]
        requested = frozenset(path for path in changed_paths if _language_for_path(path) in languages)
        seeds = requested.intersection(all_seeds)
        remaining_groups = len(active) - index
        remaining_time = max(0.0, deadline - time.monotonic())
        context = BlastContext(
            repo_path=repo_path, head_commit=head_commit, seed_paths=seeds,
            head_tree=head_tree, config=config,
            deadline=min(deadline, time.monotonic() + remaining_time / remaining_groups),
            scan_byte_budget=(
                config.max_import_scan_bytes // len(active)
                + (1 if index < config.max_import_scan_bytes % len(active) else 0)
            ),
        )
        if len(seeds) < len(requested):
            context.degrade(f"{label}_seed_not_regular_or_filtered")
        if not seeds:
            context.degrade(f"{label}_no_valid_fixed_head_seed")
        else:
            try:
                await analyzer.analyze(context, sink)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                context.degrade(f"{label}_analyzer_failed:{type(exc).__name__}")
        for language in languages:
            coverage[language] = "degraded" if context.degraded else "analyzed"
        degraded |= context.degraded
        diagnostics.extend(context.diagnostics)
        scanned_files += context.scanned_file_count
        scanned_bytes += context.scanned_bytes
    result = BlastResult(
        hints=sink.retained(), observed_hint_count=sink.observed_hint_count,
        coverage_by_language=coverage,
        truncated=sink.truncated or degraded,
        diagnostics=tuple(diagnostics),
        scanned_file_count=scanned_files, scanned_bytes=scanned_bytes,
    )
    if result.prompt_projection()["truncated"] and not result.truncated:
        result = replace(result, truncated=True)
    return result, head_tree
