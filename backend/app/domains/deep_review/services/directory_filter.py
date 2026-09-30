from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import PurePosixPath

from app.domains.deep_review.schemas.input import ChangeType, FileChange, FilterAction, FilterDecision
from app.domains.deep_review.schemas.config import DeepReviewConfig


class FilterError(ValueError):
    pass


@dataclass(frozen=True)
class FilterResult:
    decisions: list[FilterDecision]
    review_paths: list[str]
    context_paths: list[str]


def normalize_path(value: str) -> str:
    if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"):
        raise FilterError(f"invalid repository path: {value}")
    path = PurePosixPath(value.replace("\\", "/").strip())
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in path.parts[0]
    ):
        raise FilterError(f"invalid repository path: {value}")
    return path.as_posix()


@lru_cache(maxsize=1)
def _resource(name: str) -> tuple[str, ...]:
    payload = json.loads(files("app.domains.deep_review.resources").joinpath(name).read_text("utf-8"))
    return tuple(str(item).lower() for item in payload)


@lru_cache(maxsize=None)
def _expand_braces(pattern: str) -> tuple[str, ...]:
    start = pattern.find("{")
    if start < 0:
        return (pattern,)
    depth = 0
    end = -1
    comma = -1
    for index, char in enumerate(pattern[start:], start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
        elif char == "," and depth == 1 and comma < 0:
            comma = index
    if end < 0 or comma < 0:
        return (pattern,)
    prefix, body, suffix = pattern[:start], pattern[start + 1 : end], pattern[end + 1 :]
    expanded: list[str] = []
    for choice in body.split(","):
        expanded.extend(_expand_braces(prefix + choice + suffix))
    return tuple(expanded)


@lru_cache(maxsize=None)
def _pattern_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.startswith("/")
    normalized = pattern[1:] if anchored else pattern
    if normalized.startswith("**/"):
        remainder = normalized[3:]
        body = _pattern_regex(remainder[1:] if remainder.startswith("/") else remainder).pattern
        body = body.removeprefix("^").removesuffix("$")
        return re.compile("^(?:.*/)?" + body + "$", re.IGNORECASE)
    if normalized.endswith("/"):
        expression = re.escape(normalized) + ".*"
    else:
        expression = ""
        for index, segment in enumerate(normalized.split("/")):
            if index:
                expression += "/"
            if segment == "**":
                expression += ".*"
            else:
                expression += re.escape(segment).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
        if "/" not in normalized and not anchored:
            expression = "(?:^|.*/)" + expression
    return re.compile(("^" if anchored else "^") + expression + "$", re.IGNORECASE)


class DirectoryFilter:
    def __init__(self, config: DeepReviewConfig):
        self.config = config

    def _matches(self, path: str, patterns: list[str]) -> bool:
        lowered = path.lower()
        return any(_pattern_regex(item).fullmatch(lowered) for item in patterns for item in _expand_braces(item))

    def is_secret_path(self, path: str) -> bool:
        lowered = path.lower()
        basename = PurePosixPath(lowered).name
        if basename in {".env.example", ".env.sample", ".env.template"}:
            return False
        elif basename == ".env" or basename.startswith(".env."):
            return True
        return self._matches(lowered, list(_resource("default_secret_patterns.json")))

    def filter(self, changes: list[FileChange]) -> FilterResult:
        decisions: list[FilterDecision] = []
        for change in changes:
            path = normalize_path(change.path)
            is_binary = change.change_type is ChangeType.BINARY or (
                "GIT binary patch" in change.diff or "Binary files " in change.diff
            )
            if self.is_secret_path(path) or (
                change.old_path and self.is_secret_path(normalize_path(change.old_path))
            ):
                decisions.append(FilterDecision(path=path, action=FilterAction.EXCLUDE, reason="secret_path"))
                continue
            if is_binary:
                decisions.append(FilterDecision(path=path, action=FilterAction.EXCLUDE, reason="binary_file"))
                continue

            user_rule: bool | None = None
            user_rules: list[tuple[str, bool]] = [
                (pattern.strip(), False) for pattern in self.config.exclude_paths if pattern.strip()
            ]
            user_rules.extend((pattern.strip(), True) for pattern in self.config.include_paths if pattern.strip())
            for raw_pattern, desired in user_rules:
                pattern = raw_pattern[1:] if raw_pattern.startswith("!") else raw_pattern
                if pattern and self._matches(path, [pattern]):
                    user_rule = not desired if raw_pattern.startswith("!") else desired
            if user_rule is False:
                decisions.append(FilterDecision(path=path, action=FilterAction.EXCLUDE, reason="user_exclude"))
                continue
            if PurePosixPath(path).name in {".env.example", ".env.sample", ".env.template"}:
                decisions.append(FilterDecision(path=path, action=FilterAction.REVIEW, reason="env_template"))
                continue
            suffix = PurePosixPath(path).suffix.lower()
            if suffix not in _resource("supported_file_types.json"):
                decisions.append(FilterDecision(path=path, action=FilterAction.EXCLUDE, reason="unsupported_extension"))
                continue
            if user_rule is True:
                decisions.append(self._finish(change, path, "user_include"))
                continue
            if self._matches(path, list(_resource("default_exclude_patterns.json"))):
                decisions.append(FilterDecision(path=path, action=FilterAction.EXCLUDE, reason="default_exclude"))
                continue
            decisions.append(self._finish(change, path, "accepted"))
        return FilterResult(
            decisions=decisions,
            review_paths=[item.path for item in decisions if item.action is FilterAction.REVIEW],
            context_paths=[item.path for item in decisions if item.action is FilterAction.CONTEXT_ONLY],
        )

    def _finish(self, change: FileChange, path: str, accepted_reason: str) -> FilterDecision:
        if change.change_type is ChangeType.DELETED:
            return FilterDecision(path=path, action=FilterAction.CONTEXT_ONLY, reason="deleted_file")
        if len(change.diff.encode("utf-8")) > self.config.max_file_bytes:
            return FilterDecision(path=path, action=FilterAction.EXCLUDE, reason="file_too_large")
        return FilterDecision(path=path, action=FilterAction.REVIEW, reason=accepted_reason)
