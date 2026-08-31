from __future__ import annotations

from enum import StrEnum


class FindingRuntimeStack(StrEnum):
    LEGACY = "legacy"
    RUNTIME = "runtime"


_RUNTIME_ALIASES = {
    FindingRuntimeStack.LEGACY.value: FindingRuntimeStack.LEGACY,
    "old": FindingRuntimeStack.LEGACY,
    FindingRuntimeStack.RUNTIME.value: FindingRuntimeStack.RUNTIME,
    "new": FindingRuntimeStack.RUNTIME,
}


def coerce_finding_runtime_stack(value: str | None) -> FindingRuntimeStack:
    if value is None:
        return FindingRuntimeStack.LEGACY

    normalized = str(value).strip().lower()
    if not normalized:
        return FindingRuntimeStack.LEGACY

    return _RUNTIME_ALIASES.get(normalized, FindingRuntimeStack.LEGACY)
