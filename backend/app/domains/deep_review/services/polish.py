"""Safe deterministic formatting, with no model calls or factual rewriting.

Unlike PR-AF 48ae7eeb (src/pr_af/polish.py), this prototype only normalizes
whitespace and Markdown spacing. It cannot invent or move code evidence.
"""

from __future__ import annotations

import re

from app.domains.deep_review.schemas.pipeline import ReviewFinding


def _clean_block(value: str) -> str:
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    text = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def polish_finding(finding: ReviewFinding) -> ReviewFinding:
    payload = finding.model_dump(mode="python")
    payload["title"] = " ".join(finding.title.split())
    for field in ("body", "evidence", "suggestion"):
        payload[field] = _clean_block(getattr(finding, field))
    payload["tags"] = [" ".join(tag.split()) for tag in finding.tags if tag.strip()]
    return ReviewFinding.model_validate(payload)
