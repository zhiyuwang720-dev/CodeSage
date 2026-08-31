"""
Token Estimator - Token 计数器

使用 tiktoken 进行精确计数，不可用时回退到启发式估算。
"""

import logging
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

# tiktoken 编码器缓存
_encoders: dict = {}
_tiktoken_available: bool | None = None  # None=未检测, True=可用, False=不可用
_logged_method: bool = False  # 是否已输出使用方案日志


def _check_tiktoken_availability(log_result: bool = False) -> bool:
    """
    检测 tiktoken 是否可用

    Args:
        log_result: 是否输出日志（首次实际使用时输出）
    """
    global _tiktoken_available, _logged_method

    if _tiktoken_available is not None:
        # 已检测过，只在首次需要时输出日志
        if log_result and not _logged_method:
            _logged_method = True
            if _tiktoken_available:
                logger.info("✅ Token 计数方案: tiktoken 精确计数")
            else:
                logger.warning("⚠️ Token 计数方案: 启发式估算")
        return _tiktoken_available

    try:
        import tiktoken
        tiktoken.get_encoding("cl100k_base")
        _tiktoken_available = True
        if log_result:
            _logged_method = True
            logger.info("✅ Token 计数方案: tiktoken 精确计数")
    except ImportError:
        _tiktoken_available = False
        if log_result:
            _logged_method = True
            logger.warning("⚠️ Token 计数方案: 启发式估算 (tiktoken 未安装)")
    except Exception as e:
        _tiktoken_available = False
        if log_result:
            _logged_method = True
            logger.warning(f"⚠️ Token 计数方案: 启发式估算 (tiktoken 初始化失败: {e})")

    return _tiktoken_available


# 模块加载时静默检测（不输出日志）
_check_tiktoken_availability(log_result=False)


def _get_tiktoken_encoder(model: str):
    """
    获取 tiktoken 编码器（带缓存）

    Args:
        model: 模型名称

    Returns:
        tiktoken 编码器或 None
    """
    if model in _encoders:
        return _encoders[model]

    try:
        import tiktoken

        # 尝试按模型名获取编码器
        try:
            encoder = tiktoken.encoding_for_model(model)
        except KeyError:
            # 未知模型，使用 cl100k_base（GPT-4/3.5 使用的编码）
            encoder = tiktoken.get_encoding("cl100k_base")

        _encoders[model] = encoder
        return encoder
    except ImportError:
        logger.debug("tiktoken not available, using heuristic estimation")
        _encoders[model] = None
        return None
    except Exception as e:
        logger.warning(f"Failed to get tiktoken encoder: {e}")
        _encoders[model] = None
        return None


class TokenEstimator:
    """Token 估算器"""

    @staticmethod
    def count_tokens(text: str, model: str = "gpt-4") -> int:
        """
        计算文本的 token 数量

        优先使用 tiktoken 精确计数，不可用时使用启发式估算。

        Args:
            text: 要计算的文本
            model: 模型名称（用于选择正确的编码器）

        Returns:
            Token 数量
        """
        if not text:
            return 0

        # 首次调用时输出使用方案日志
        _check_tiktoken_availability(log_result=True)

        # 尝试使用 tiktoken
        encoder = _get_tiktoken_encoder(model)
        if encoder is not None:
            try:
                return len(encoder.encode(text))
            except Exception as e:
                logger.debug(f"tiktoken encode failed: {e}, falling back to heuristic")

        # 启发式估算
        return TokenEstimator._heuristic_estimate(text)

    @staticmethod
    def _heuristic_estimate(text: str) -> int:
        """
        启发式 token 估算

        基于字符类型的估算规则：
        - 英文/ASCII: ~4 字符/token
        - 中文/CJK: ~1.5 字符/token（中文分词后每个词约 1-2 token）
        - 其他 Unicode: ~2 字符/token

        Args:
            text: 文本内容

        Returns:
            估算的 token 数量
        """
        if not text:
            return 0

        ascii_chars = 0
        cjk_chars = 0
        other_chars = 0

        for char in text:
            code = ord(char)
            if code < 128:
                ascii_chars += 1
            elif 0x4E00 <= code <= 0x9FFF:  # CJK 统一汉字
                cjk_chars += 1
            elif 0x3400 <= code <= 0x4DBF:  # CJK 扩展 A
                cjk_chars += 1
            elif 0x20000 <= code <= 0x2A6DF:  # CJK 扩展 B
                cjk_chars += 1
            elif 0x3000 <= code <= 0x303F:  # CJK 标点
                cjk_chars += 1
            elif 0xFF00 <= code <= 0xFFEF:  # 全角字符
                cjk_chars += 1
            else:
                other_chars += 1

        # 估算公式
        tokens = (
            ascii_chars / 4.0 +      # 英文约 4 字符/token
            cjk_chars / 1.5 +        # 中文约 1.5 字符/token
            other_chars / 2.0        # 其他约 2 字符/token
        )

        # 至少返回 1
        return max(1, int(tokens + 0.5))

    @staticmethod
    def estimate_messages_tokens(messages: list, model: str = "gpt-4") -> int:
        """
        估算消息列表的 token 数量

        包括消息格式开销（role、分隔符等）

        Args:
            messages: 消息列表，每条消息包含 role 和 content
            model: 模型名称

        Returns:
            总 token 数量
        """
        total = 0

        for msg in messages:
            # 每条消息的格式开销约 4 tokens
            total += 4

            content = msg.get("content", "")
            if isinstance(content, str):
                total += TokenEstimator.count_tokens(content, model)
            elif isinstance(content, list):
                # 多模态消息
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total += TokenEstimator.count_tokens(part.get("text", ""), model)

        # 消息列表的额外开销
        total += 3

        return total
