"""模型边界异常：只保留有调用者的恢复类别（P06）。

旧 `models/errors.py` 的完整 Agent/Tool/State/Validation 继承树在本层没有调用者，
已删除；这里只保留 RuntimeStopReason / recoverable_error_kind 需要的类别。
"""

from __future__ import annotations

from typing import Optional

__all__ = [
    "ModelBoundaryError",
    "ModelAuthenticationError",
    "ModelBadRequestError",
    "ModelConnectionError",
    "ModelQuotaExceededError",
    "ModelRateLimitError",
    "ModelResponseError",
    "ModelStreamTimeoutError",
    "ModelTimeoutError",
]

RECOVERABLE_ERROR_KINDS = frozenset({"rate_limit", "connection", "timeout", "stream_timeout"})


class ModelBoundaryError(RuntimeError):
    """模型边界错误基类：只携带恢复决策所需的字段。"""

    error_code = "MODEL_ERROR"
    recoverable = False
    kind = "unknown"

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        cause: Optional[BaseException] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.cause = cause


class ModelAuthenticationError(ModelBoundaryError):
    error_code = "MODEL_AUTH"
    kind = "authentication"


class ModelBadRequestError(ModelBoundaryError):
    error_code = "MODEL_BAD_REQUEST"
    kind = "invalid_request"


class ModelQuotaExceededError(ModelBoundaryError):
    error_code = "MODEL_QUOTA_EXCEEDED"
    kind = "quota_exceeded"


class ModelRateLimitError(ModelBoundaryError):
    error_code = "MODEL_RATE_LIMIT"
    recoverable = True
    kind = "rate_limit"


class ModelTimeoutError(ModelBoundaryError):
    error_code = "MODEL_TIMEOUT"
    recoverable = True
    kind = "timeout"


class ModelStreamTimeoutError(ModelTimeoutError):
    error_code = "MODEL_STREAM_TIMEOUT"
    kind = "stream_timeout"


class ModelConnectionError(ModelBoundaryError):
    error_code = "MODEL_CONNECTION"
    recoverable = True
    kind = "connection"


class ModelResponseError(ModelBoundaryError):
    error_code = "MODEL_RESPONSE"
    kind = "invalid_response"
