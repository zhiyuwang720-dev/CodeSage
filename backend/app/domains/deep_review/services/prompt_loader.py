from __future__ import annotations

import re
from functools import lru_cache
from importlib.resources import files

_PROMPTS = {
    "semantic", "semantic_user", "planner", "planner_user",
    "reviewer", "reviewer_fallback", "cross_analysis",
}
_PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    if name not in _PROMPTS:
        raise ValueError(f"Unknown deep review prompt: {name}")
    return files("app.domains.deep_review.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")


def render_prompt(name: str, **values: str) -> str:
    template = load_prompt(name)
    placeholders = _PLACEHOLDER.findall(template)
    if len(placeholders) != len(set(placeholders)) or set(placeholders) != set(values):
        raise ValueError(f"invalid prompt placeholders for {name}")
    for key, value in values.items():
        if not isinstance(value, str):
            raise TypeError(f"prompt value {key} must be a string")
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)
