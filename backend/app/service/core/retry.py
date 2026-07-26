"""
Retry Mechanism Module

Production-grade retry with exponential backoff for the Agent framework.
"""

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from functools import wraps
from typing import Any, Awaitable, Callable, Generic, Optional, Tuple, Type, TypeVar

from .errors import (
    AgentError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMConnectionError,
    ToolTimeoutError,
    ToolResourceError,
    is_recoverable,
    get_retry_after,
)

T = TypeVar("T")


class BackoffStrategy(str, Enum):
    """Backoff strategies"""
    CONSTANT = "constant"         # 恒定延迟
    LINEAR = "linear"             # 线性增长
    EXPONENTIAL = "exponential"   # 指数增长


@dataclass
class RetryConfig:
    """Retry configuration"""
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0      # 指数基数
    jitter: bool = True                     # 是否启用抖动
    jitter_factor: float = 0.5         # 抖动因子
    backoff_strategy: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    retryable_exceptions: Tuple[Type[Exception], ...] = (
        LLMRateLimitError, LLMTimeoutError, LLMConnectionError,
        ToolTimeoutError, ToolResourceError,
        ConnectionError, TimeoutError, asyncio.TimeoutError,
    )

    def should_retry(self, error: Exception) -> bool:
        if isinstance(error, self.retryable_exceptions):
            return True
        if isinstance(error, AgentError):
            return error.recoverable
        return False

    def calculate_delay(self, attempt: int, error: Optional[Exception] = None) -> float:
        """优先级: 错误指定的 retry_after > 退避策略计算 > 默认"""
        if error:
            # 如果错误指定了 retry_after，优先使用
            retry_after = get_retry_after(error)
            if retry_after:
                return min(float(retry_after), self.max_delay)

        if self.backoff_strategy == BackoffStrategy.CONSTANT:
            delay = self.base_delay
        elif self.backoff_strategy == BackoffStrategy.LINEAR:
            delay = self.base_delay * (attempt + 1)
        else:
            delay = self.base_delay * (self.exponential_base ** attempt)

        delay = min(delay, self.max_delay)

        if self.jitter:
            # 添加抖动
            jitter_range = delay * self.jitter_factor
            delay = delay + random.uniform(-jitter_range, jitter_range)
            delay = max(0.1, delay)

        return delay


# Predefined configs
# LLM 调用：3次尝试，1s起步，最大60s
LLM_RETRY_CONFIG = RetryConfig(max_attempts=3, base_delay=1.0, max_delay=60.0)
# 工具调用：2次尝试，2s起步，最大30s
TOOL_RETRY_CONFIG = RetryConfig(max_attempts=2, base_delay=2.0, max_delay=30.0)
# 不重试：1次尝试，延迟为0
NO_RETRY_CONFIG = RetryConfig(max_attempts=1, base_delay=0, max_delay=0)


@dataclass
class RetryResult(Generic[T]):
    """Result of retry operation"""
    success: bool
    value: Optional[T] = None
    error: Optional[Exception] = None
    attempts: int = 0
    total_delay: float = 0.0


async def retry_with_backoff(
    func: Callable[[], Awaitable[T]],
    config: RetryConfig = RetryConfig(),
    on_retry: Optional[Callable[[int, Exception, float], Awaitable[None]]] = None,
    operation_name: str = "operation",
) -> T:
    """Execute async function with retry and exponential backoff."""
    last_exception: Optional[Exception] = None
    total_delay = 0.0

    for attempt in range(config.max_attempts):
        try:
            return await func()
        except Exception as e:
            last_exception = e

            if not config.should_retry(e):
                raise

            if attempt >= config.max_attempts - 1:
                raise

            delay = config.calculate_delay(attempt, e)
            total_delay += delay

            if on_retry:
                await on_retry(attempt + 1, e, delay)

            await asyncio.sleep(delay)

    if last_exception:
        raise last_exception
    raise RuntimeError(f"{operation_name} failed")


async def retry_with_result(
    func: Callable[[], Awaitable[T]],
    config: RetryConfig = RetryConfig(),
) -> RetryResult[T]:
    """安全版本, 返回 RetryResult, 需要优雅处理失败"""
    total_delay = 0.0
    last_exception: Optional[Exception] = None

    for attempt in range(config.max_attempts):
        try:
            result = await func()
            return RetryResult(success=True, value=result, attempts=attempt + 1, total_delay=total_delay)
        except Exception as e:
            last_exception = e
            if not config.should_retry(e) or attempt >= config.max_attempts - 1:
                return RetryResult(success=False, error=e, attempts=attempt + 1, total_delay=total_delay)
            delay = config.calculate_delay(attempt, e)
            total_delay += delay
            await asyncio.sleep(delay)

    return RetryResult(success=False, error=last_exception, attempts=config.max_attempts, total_delay=total_delay)


def with_retry(config: Optional[RetryConfig] = None, operation_name: Optional[str] = None):
    """Decorator to add retry behavior."""
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            return await retry_with_backoff(
                lambda: func(*args, **kwargs),
                config=config or RetryConfig(),
                operation_name=operation_name or func.__name__,
            )
        return wrapper
    return decorator


class RetryContext:
    """Context manager for retry operations."""

    def __init__(self, config: RetryConfig = RetryConfig(), operation_name: str = "operation"):
        self.config = config
        self.operation_name = operation_name
        self.attempt = 0
        self.total_delay = 0.0
        self.last_error: Optional[Exception] = None
        self.result: Optional[Any] = None
        self.success = False
        self._should_continue = True

    async def __aenter__(self) -> "RetryContext":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    def should_continue(self) -> bool:
        return self._should_continue and self.attempt < self.config.max_attempts

    def record_success(self, result: Any) -> None:
        self.success = True
        self.result = result
        self._should_continue = False

    async def record_failure(self, error: Exception) -> bool:
        self.last_error = error
        self.attempt += 1

        if not self.config.should_retry(error) or self.attempt >= self.config.max_attempts:
            self._should_continue = False
            return False

        delay = self.config.calculate_delay(self.attempt - 1, error)
        self.total_delay += delay
        await asyncio.sleep(delay)
        return True

    def get_result(self) -> RetryResult:
        return RetryResult(
            success=self.success,
            value=self.result,
            error=self.last_error,
            attempts=self.attempt,
            total_delay=self.total_delay,
        )

"""
RetryContext 使用示例:
async def fetch_data_with_retry():
    config = RetryConfig(max_attempts=3, base_delay=1.0)
    
    async with RetryContext(config) as ctx:
        while ctx.should_continue():
            try:
                # 尝试执行危险操作
                data = await fetch_from_api()
                ctx.record_success(data)  # 成功，记录结果
            except Exception as e:
                # 失败，记录错误，决定是否重试
                should_retry = await ctx.record_failure(e)
                if not should_retry:
                    break  # 无法重试，退出循环
    
    result = ctx.get_result()
    return result
"""