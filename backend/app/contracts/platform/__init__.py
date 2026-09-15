"""Versioned, transport-neutral platform boundary contracts."""

from .execution import (
    AttemptContext,
    InputArtifactRef,
    ResultManifestRef,
    ResultSubmission,
    RunCommand,
    SubmissionReceipt,
)

__all__ = [
    "AttemptContext", "InputArtifactRef", "ResultManifestRef",
    "ResultSubmission", "RunCommand", "SubmissionReceipt",
]
