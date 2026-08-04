"""Provider adapters: transport + wire-format conversion only."""

from .anthropic import AnthropicAdapter
from .base import BaseAdapter
from .openai_compatible import OpenAICompatibleAdapter

__all__ = ["AnthropicAdapter", "BaseAdapter", "OpenAICompatibleAdapter"]
