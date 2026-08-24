"""适配器契约层:BaseAdapter 是唯一驻留契约层的适配器形状。

具体提供者的适配器(deepseek 系、anthropic、回放)住在各自的
独立包(llm_deepseek / llm_anthropic / llm_replay),经能力接缝
注册进来 —— 契约层只定义「适配器长什么样」,不定义「有哪些」。
"""

from .base import BaseAdapter

__all__ = ["BaseAdapter"]
