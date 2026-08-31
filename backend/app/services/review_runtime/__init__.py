"""Finding runtime package for the ongoing migration."""

from .config import FindingRuntimeStack, coerce_finding_runtime_stack
from .models import RuntimeMessageRole, RuntimeSessionState, RuntimeStopReason, TranscriptItem

__all__ = [
    "FindingRuntimeBridge",
    "FindingRuntimeStack",
    "RuntimeMessageRole",
    "RuntimeSessionState",
    "RuntimeStopReason",
    "TranscriptItem",
    "coerce_finding_runtime_stack",
]


def __getattr__(name: str):
    if name == "FindingRuntimeBridge":
        from .bridge import FindingRuntimeBridge

        return FindingRuntimeBridge
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")