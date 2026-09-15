from __future__ import annotations

from enum import StrEnum


class RuntimeStack(StrEnum):
    LEGACY = "legacy"
    RUNTIME = "runtime"


_RUNTIME_ALIASES = {
    RuntimeStack.LEGACY.value: RuntimeStack.LEGACY,
    "old": RuntimeStack.LEGACY,
    RuntimeStack.RUNTIME.value: RuntimeStack.RUNTIME,
    "new": RuntimeStack.RUNTIME,
}


def coerce_runtime_stack(value: str | None) -> RuntimeStack:
    if value is None:
        return RuntimeStack.LEGACY

    normalized = str(value).strip().lower()
    if not normalized:
        return RuntimeStack.LEGACY

    return _RUNTIME_ALIASES.get(normalized, RuntimeStack.LEGACY)
