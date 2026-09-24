from __future__ import annotations

from collections import Counter

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.pipeline import (
    CrossReferenceHint,
    ReviewDimension,
    ReviewInvestigation,
    ReviewPlan,
)
from app.domains.deep_review.services.input_builder import ReviewSnapshot
from app.domains.deep_review.services.directory_filter import DirectoryFilter


def _name_key(dimension: ReviewDimension, path_indexes: dict[str, int]) -> tuple:
    target_index = min((path_indexes.get(path, 10**9) for path in dimension.target_files), default=10**9)
    return (
        dimension.priority,
        1 if dimension.fallback else 0,
        target_index,
        dimension.name,
    )


def _chunks(paths: list[str], size: int) -> list[list[str]]:
    return [paths[index : index + size] for index in range(0, len(paths), size)]


def _max_unsplit_files(config: DeepReviewConfig) -> int:
    """Allow up to one-third over the nominal cap before splitting a work item."""
    nominal_limit = config.max_files_per_work_item
    return nominal_limit + nominal_limit // 3


def _unique_name(base: str, used: set[str]) -> str:
    name = base[:64]
    suffix = 2
    while name in used:
        tail = f"_{suffix}"
        name = base[: 64 - len(tail)] + tail
        suffix += 1
    used.add(name)
    return name


def repair_plan(
    plan_draft: dict,
    snapshot: ReviewSnapshot,
    config: DeepReviewConfig,
    *,
    legal_context_paths: set[str] | None = None,
) -> ReviewPlan:
    review_paths = sorted(set(snapshot.review_paths))
    path_indexes = {path: index for index, path in enumerate(review_paths)}
    allowed = set(review_paths)
    context_candidates = set(legal_context_paths if legal_context_paths is not None else snapshot.allowed_paths)
    secret_filter = DirectoryFilter(config)
    actions: list[str] = []
    model_dimensions: list[tuple[int, ReviewDimension]] = []
    lineage: dict[str, set[str]] = {}
    used_names: set[str] = set()
    name_counts = Counter(str(item.get("name") or "").strip() for item in plan_draft.get("dimensions") or [])

    for original_index, item in enumerate(plan_draft.get("dimensions") or []):
        requested_name = str(item.get("name") or "").strip()
        base_name = requested_name or f"dimension_{original_index + 1}"

        targets: list[str] = []
        for path in item.get("target_files") or []:
            if path not in allowed:
                actions.append(f"removed_invalid_target:{base_name}:{path}")
                continue
            if path in targets:
                actions.append(f"removed_repeated_target:{base_name}:{path}")
                continue
            targets.append(path)
        if not targets:
            actions.append(f"dropped_empty_dimension:{base_name}")
            continue
        targets.sort(key=path_indexes.__getitem__)

        name = _unique_name(base_name, used_names)
        if name != base_name:
            actions.append(f"renamed_duplicate_dimension:{base_name}->{name}")

        contexts = sorted(
            {
                path
                for path in (item.get("context_files") or [])
                if path in context_candidates and path not in targets and not secret_filter.is_secret_path(path)
            }
        )
        model_dimensions.append(
            (original_index, ReviewDimension(
                name=name,
                investigations=[ReviewInvestigation(
                    source_dimension=name,
                    review_prompt=str(item.get("review_prompt") or "").strip(),
                    priority=int(item.get("priority") or 5),
                    rationale=str(item.get("rationale") or ""),
                )],
                target_files=targets,
                context_files=contexts,
                priority=int(item.get("priority") or 5),
                source="model",
            ))
        )
        lineage[name] = {base_name}

    # A later investigation is absorbed only when its entire target set is already
    # covered by one retained dimension. Partial overlaps remain coherent work items.
    retained: list[ReviewDimension] = []
    for _, dimension in sorted(model_dimensions, key=lambda pair: (pair[1].priority, pair[0])):
        target_set = set(dimension.target_files)
        hosts = [
            host for host in retained if target_set.issubset(host.target_files)
        ]
        if not hosts:
            retained.append(dimension)
            continue
        host = hosts[0]  # retained is already ordered by priority, then Draft position
        host.investigations.extend(dimension.investigations)
        host.context_files = sorted(
            (set(host.context_files) | set(dimension.context_files) | target_set)
            - set(host.target_files)
        )
        host.source = "merged"
        lineage[host.name].update(lineage[dimension.name])
        actions.append(f"merged_contained_dimension:{dimension.name}->{host.name}")

    covered = {path for dimension in retained for path in dimension.target_files}
    missing = [path for path in review_paths if path not in covered]
    if missing:
        fallback_name = _unique_name("fallback", used_names)
        retained.append(
            ReviewDimension(
                name=fallback_name,
                investigations=[ReviewInvestigation(
                    source_dimension=fallback_name,
                    review_prompt=(
                    "Review each target diff for correctness, security, architecture and quality. "
                    "Trace direct callers and contracts, then report only defects caused by this PR."
                    ),
                    priority=10,
                )],
                target_files=missing,
                context_files=[],
                priority=10,
                fallback=True,
                source="fallback",
            )
        )
        lineage[fallback_name] = set()
        actions.append(f"added_fallback_dimension:{fallback_name}")

    dimensions: list[ReviewDimension] = []
    max_unsplit_files = _max_unsplit_files(config)
    for dimension in retained:
        if len(dimension.target_files) <= max_unsplit_files:
            dimensions.append(dimension)
            continue
        for index, paths in enumerate(_chunks(dimension.target_files, config.max_files_per_work_item), start=1):
            name = _unique_name(f"{dimension.name}_split_{index:03d}", used_names)
            dimensions.append(
                ReviewDimension(
                    name=name,
                    investigations=[item.model_copy(deep=True) for item in dimension.investigations],
                    target_files=paths,
                    context_files=list(dimension.context_files),
                    priority=dimension.priority,
                    fallback=dimension.fallback,
                    source="split",
                )
            )
            lineage[name] = set(lineage[dimension.name])
            actions.append(f"split_dimension:{dimension.name}->{name}")

    active = [item for item in dimensions if not item.deferred]
    deferred: list[ReviewDimension] = []
    max_final = config.max_final_dimensions
    while len(active) > max_final:
        active.sort(key=lambda item: _name_key(item, path_indexes))
        postponed = active.pop()
        deferred.append(postponed.model_copy(update={"deferred": True}))
        actions.append(f"deferred_dimension:{postponed.name}")

    dimensions = sorted(active, key=lambda item: _name_key(item, path_indexes)) + list(
        reversed(deferred)
    )
    final_by_original: dict[str, list[str]] = {}
    for dimension in dimensions:
        for original in lineage[dimension.name]:
            mapped = final_by_original.setdefault(original, [])
            if dimension.name not in mapped:
                mapped.append(dimension.name)
    hints: list[CrossReferenceHint] = []
    unresolved_risks: list[str] = []
    hint_keys: set[tuple[tuple[str, ...], str, str]] = set()
    for hint in plan_draft.get("cross_reference_hints") or []:
        names = list(hint.get("dimension_names") or [])
        if len(set(names)) < 2 or any(
            name_counts[name] != 1 or len(final_by_original.get(name, [])) != 1
            for name in names
        ):
            actions.append("dropped_invalid_cross_reference_hint")
            unresolved_risks.append(f"Cross-reference hint could not be mapped: {names}")
            continue
        mapped_names = list(dict.fromkeys(final_by_original[name][0] for name in names))
        if any(next(dim for dim in dimensions if dim.name == name).deferred for name in mapped_names):
            actions.append("dropped_deferred_cross_reference_hint")
            unresolved_risks.append(f"Cross-reference hint deferred: {names}")
            continue
        relation = str(hint.get("relation") or "")
        symbol = str(hint.get("symbol_or_contract") or "")
        if len(mapped_names) == 1:
            host = next(dim for dim in dimensions if dim.name == mapped_names[0])
            host.investigations.append(ReviewInvestigation(
                source_dimension="cross_reference_hint",
                review_prompt=f"Check the cross-reference relation: {relation}; symbol or contract: {symbol}",
                priority=10,
            ))
            actions.append(f"folded_internal_cross_reference_hint:{host.name}")
            continue
        key = (tuple(mapped_names), relation, symbol)
        if key not in hint_keys:
            hints.append(CrossReferenceHint(
                dimension_names=mapped_names, relation=relation, symbol_or_contract=symbol,
            ))
            hint_keys.add(key)

    for dimension in deferred:
        unresolved_risks.append(
            f"Deferred review dimension {dimension.name}: {', '.join(dimension.target_files)}"
        )
    all_targets = {path for item in dimensions for path in item.target_files}
    if all_targets != allowed:
        raise ValueError("repaired plan must cover every review file")
    if any(len(item.target_files) > max_unsplit_files for item in dimensions):
        raise ValueError("repaired plan exceeds the unsplit dimension file limit")
    active_targets = {path for item in active for path in item.target_files}
    coverage_complete = not deferred and active_targets == allowed and not unresolved_risks

    return ReviewPlan(
        summary=str(plan_draft.get("summary") or ""),
        dimensions=dimensions,
        cross_reference_hints=hints,
        dimension_name_map=final_by_original,
        assumptions=[str(item) for item in (plan_draft.get("assumptions") or [])],
        coverage_complete=coverage_complete,
        repair_actions=actions,
        unresolved_risks=unresolved_risks,
    )
