"""Spec 20 acceptance fixtures.

The model/HTTP fixture is intentionally shared with the P0 model-boundary
suite.  Plan 20 adds a different evidence root and higher-level assertions,
but it must exercise the same real LiteLLM SDK and real local HTTP/SSE server.
"""

from __future__ import annotations

import datetime
import os
import subprocess
from pathlib import Path

import pytest

pytest_plugins = ["tests.observability_acceptance.model_harness"]


def _plan20_artifact_root() -> Path:
    configured = os.environ.get("CODESAGE_PLAN20_ARTIFACT_ROOT")
    if configured:
        return Path(configured)
    backend = Path(__file__).resolve().parents[2]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(backend),
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        commit = "unknown"
    return backend / ".acceptance-artifacts" / "plan20" / f"{stamp}-{commit}"


@pytest.fixture(scope="session")
def acceptance_artifact_root() -> Path:
    """Override the P0 fixture's root without duplicating the harness."""

    root = _plan20_artifact_root()
    root.mkdir(parents=True, exist_ok=True)
    return root
