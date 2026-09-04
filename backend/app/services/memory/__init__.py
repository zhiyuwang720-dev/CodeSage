"""记忆/上下文注入层(06-P5): 独立 services/memory/, 与编排解耦。

runtime.py 承载 RuntimeMemoryManager(指令记忆来自审计规则集/项目记忆文件,
技能库召回参考来自 code-audit-finding 技能目录)与记忆渲染 helper。
"""
from app.services.memory.runtime import (
    RUNTIME_MEMORY_HEADER,
    RuntimeMemoryManager,
    build_memory_message,
    build_runtime_memory_prompt,
    strip_runtime_memory_section,
)

__all__ = [
    "RUNTIME_MEMORY_HEADER",
    "RuntimeMemoryManager",
    "build_memory_message",
    "build_runtime_memory_prompt",
    "strip_runtime_memory_section",
]
