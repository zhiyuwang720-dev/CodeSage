from __future__ import annotations

from collections import Counter

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.pipeline import (
    CrossReferenceHint,
    ReviewDimension,
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
    model_dimensions: list[ReviewDimension] = []
    lineage: dict[str, set[str]] = {}
    owner: dict[str, str] = {}
    used_names: set[str] = set()
    name_counts = Counter(str(item.get("name") or "") for item in plan_draft.get("dimensions") or [])

    for original_index, item in enumerate(plan_draft.get("dimensions") or []):
        requested_name = str(item.get("name") or "").strip()
        base_name = requested_name or f"dimension_{original_index + 1}"

        targets: list[str] = []
        for path in item.get("target_files") or []:
            if path not in allowed:
                actions.append(f"removed_invalid_target:{base_name}:{path}")
                continue
            if path in owner:
                actions.append(f"removed_duplicate_target:{base_name}:{path}")
                continue
            owner[path] = base_name
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
            ReviewDimension(
                name=name,
                review_prompt=str(item.get("review_prompt") or "").strip(),
                target_files=targets,
                context_files=contexts,
                priority=int(item.get("priority") or 5),
                source="model",
            )
        )
        lineage[name] = {base_name}

    missing = [path for path in review_paths if path not in owner]
    if missing:
        fallback_name = _unique_name("fallback", used_names)
        model_dimensions.append(
            ReviewDimension(
                name=fallback_name,
                review_prompt=(
                    "Review each target diff for correctness, security, architecture and quality. "
                    "Trace direct callers and contracts, then report only defects caused by this PR."
                ),
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
    for dimension in model_dimensions:
        if len(dimension.target_files) <= config.max_files_per_work_item:
            dimensions.append(dimension)
            continue
        for index, paths in enumerate(_chunks(dimension.target_files, config.max_files_per_work_item), start=1):
            name = _unique_name(f"{dimension.name}_split_{index:03d}", used_names)
            dimensions.append(
                ReviewDimension(
                    name=name,
                    review_prompt=dimension.review_prompt,
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
        merged = False
        for index in range(len(active) - 2, -1, -1):
            left, right = active[index], active[index + 1]
            combined_targets = left.target_files + right.target_files
            if len(combined_targets) > config.max_files_per_work_item:
                continue
            name = _unique_name(f"merged_{left.name}_{right.name}", used_names)
            merged_dimension = ReviewDimension(
                name=name,
                review_prompt=(
                    f"{left.review_prompt}\n\nAdditionally: {right.review_prompt}"
                )[:2000],
                target_files=sorted(combined_targets, key=path_indexes.__getitem__),
                context_files=sorted(
                    (set(left.context_files) | set(right.context_files)) - set(combined_targets)
                ),
                priority=min(left.priority, right.priority),
                fallback=left.fallback or right.fallback,
                source="merged",
            )
            active[index : index + 2] = [merged_dimension]
            lineage[name] = lineage[left.name] | lineage[right.name]
            actions.append(f"merged_dimensions:{left.name}+{right.name}->{name}")
            merged = True
            break
        if not merged:
            postponed = active.pop()
            deferred.append(ReviewDimension(**{**postponed.model_dump(), "deferred": True}))
            actions.append(f"deferred_dimension:{postponed.name}")

    dimensions = sorted(active, key=lambda item: _name_key(item, path_indexes)) + list(
        reversed(deferred)
    )
    final_by_original: dict[str, set[str]] = {}
    for dimension in dimensions:
        for original in lineage[dimension.name]:
            final_by_original.setdefault(original, set()).add(dimension.name)
    hints: list[CrossReferenceHint] = []
    for hint in plan_draft.get("cross_reference_hints") or []:
        names = list(hint.get("dimension_names") or [])
        if len(set(names)) < 2 or any(
            name_counts[name] != 1 or len(final_by_original.get(name, set())) != 1
            for name in names
        ):
            actions.append("dropped_invalid_cross_reference_hint")
            continue
        mapped_names = list(dict.fromkeys(next(iter(final_by_original[name])) for name in names))
        if len(mapped_names) < 2:
            actions.append("dropped_invalid_cross_reference_hint")
            continue
        hints.append(
            CrossReferenceHint(
                dimension_names=mapped_names,
                relation=str(hint.get("relation") or ""),
                symbol_or_contract=str(hint.get("symbol_or_contract") or ""),
            )
        )

    unresolved_risks: list[str] = []
    for dimension in deferred:
        unresolved_risks.append(
            f"Deferred review dimension {dimension.name}: {', '.join(dimension.target_files)}"
        )
    all_targets = [path for item in dimensions for path in item.target_files]
    if len(all_targets) != len(set(all_targets)) or set(all_targets) != allowed:
        raise ValueError("repaired plan must assign exactly one owner to each review file")
    if any(len(item.target_files) > config.max_files_per_work_item for item in dimensions):
        raise ValueError("repaired plan exceeds the dimension file limit")
    coverage_complete = not deferred

    return ReviewPlan(
        summary=str(plan_draft.get("summary") or ""),
        dimensions=dimensions,
        cross_reference_hints=hints,
        assumptions=[str(item) for item in (plan_draft.get("assumptions") or [])],
        coverage_complete=coverage_complete,
        repair_actions=actions,
        unresolved_risks=unresolved_risks,
    )
