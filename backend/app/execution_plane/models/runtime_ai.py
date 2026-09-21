"""RuntimeBridge AI/Harness result and error contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


class AIParseError(ValueError):
    def __init__(self, message: str, *, raw_text: str):
        super().__init__(message)
        self.raw_text = raw_text


class AISchemaValidationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        raw_text: str,
        validation_errors: list[dict[str, Any]],
    ):
        super().__init__(message)
        self.raw_text = raw_text
        self.validation_errors = validation_errors


class HarnessIncompleteError(RuntimeError):
    pass


@dataclass
class AIResult:
    parsed: BaseModel | None
    raw_text: str
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None

    @property
    def text(self) -> str:
        return self.raw_text

    @property
    def content(self) -> str:
        return self.raw_text

    def __getattr__(self, name: str) -> Any:
        parsed = self.__dict__.get("parsed")
        if parsed is None:
            raise AttributeError(name)
        return getattr(parsed, name)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        if self.parsed is None:
            return {
                "text": self.raw_text,
                "usage": self.usage,
                "cost_usd": self.cost_usd,
            }
        return self.parsed.model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:
        if self.parsed is None:
            import json

            return json.dumps(self.model_dump(), ensure_ascii=False)
        return self.parsed.model_dump_json(**kwargs)


@dataclass
class HarnessResult:
    parsed: BaseModel
    session_id: str
    result: dict[str, Any]
    usage: dict[str, Any] | None
    cost_usd: float | None
