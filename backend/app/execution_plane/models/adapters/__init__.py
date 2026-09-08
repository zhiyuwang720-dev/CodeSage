"""
LLM适配器模块

适配器分为两类:
1. LiteLLM 统一适配器 - 支持 OpenAI, Claude, Gemini, DeepSeek, Qwen, Zhipu, Moonshot, Ollama
2. 原生适配器 - 用于 API 格式特殊的提供商: Baidu, MiniMax, Doubao
"""

# LiteLLM 统一适配器
from .litellm_adapter import LiteLLMAdapter
from .anthropic_adapter import AnthropicAdapter
from .openai_responses_adapter import OpenAIResponsesAdapter
from .gemini_native_adapter import GeminiNativeAdapter

# 原生适配器 (用于 API 格式特殊的提供商)
from .baidu_adapter import BaiduAdapter
from .minimax_adapter import MinimaxAdapter
from .doubao_adapter import DoubaoAdapter

__all__ = [
    "LiteLLMAdapter",
    "AnthropicAdapter",
    "OpenAIResponsesAdapter",
    "GeminiNativeAdapter",
    "BaiduAdapter",
    "MinimaxAdapter",
    "DoubaoAdapter",
]
