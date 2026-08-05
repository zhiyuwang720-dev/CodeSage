"""Repository-root package shim.

Project layout: the repo root directory is `codesage/` and the real package
lives at `codesage/codesage/`. Without this shim, running `python -m
codesage.cli` from the repo root makes Python treat the root directory as a
namespace package — `from .. import __version__` then resolves against the
empty namespace and fails.

This shim makes `import codesage` and `python -m codesage.cli` work from the
repo root (and any directory) by re-exporting the real package and aliasing
its submodules. Inside `codesage/` (the project dir) or via the installed
console script `codesage`, the real package is resolved directly and this
file is never touched.
"""

from __future__ import annotations

import importlib
import sys

from codesage.codesage import *  # noqa: F401,F403 — re-export public API
from codesage.codesage import __version__

# Submodule aliases so `python -m codesage.cli` / `import codesage.cli`
# resolve to the real package's modules regardless of the working directory.
for _name in ("cli", "config", "ai", "tools", "core", "permissions", "engine"):
    sys.modules[f"{__name__}.{_name}"] = importlib.import_module(f"codesage.codesage.{_name}")
