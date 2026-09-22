from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

_PROMPTS = {"semantic", "planner", "reviewer", "reviewer_fallback", "cross_analysis"}


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    if name not in _PROMPTS:
        raise ValueError(f"Unknown deep review prompt: {name}")
    return files("app.domains.deep_review.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")

