"""观测验收夹具注册（pytest 自动加载）。"""

from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR.parent))

from tests.observability_acceptance.model_harness import (  # noqa: E402
    acceptance_artifact_root,
    model_harness,
)

__all__ = ["acceptance_artifact_root", "model_harness"]
