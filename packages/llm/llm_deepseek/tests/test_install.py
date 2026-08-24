"""install 端到端:提供者包经能力接缝注册到 llm 服务。

验证闭环:组合里挂上 llm 服务 → install 本包 → 提供者名可解析、
配置可解析。不发真实请求(无 key 不触网)。
"""

import asyncio
import sys
from pathlib import Path

_FAMILY = Path(__file__).resolve().parents[2]  # 家族根 packages/llm
_ROOT = Path(__file__).resolve().parents[4]  # 仓库根(CodeSage)
sys.path.insert(0, str(_FAMILY))
sys.path.insert(0, str(_ROOT / "cordis-py"))

from cordis import Context  # noqa: E402

from llm import LLMService, LlmCallConfig  # noqa: E402

from llm_deepseek import PROVIDERS, install  # noqa: E402


async def _install_registers_and_resolves():
    ctx = Context()
    LLMService(ctx)
    install(ctx)
    for name in PROVIDERS:
        assert name in ctx.llm.list_providers()
    config = ctx.llm.resolve_call_config("deepseek:deepseek-chat")
    assert config == LlmCallConfig(provider="deepseek", model="deepseek-chat")
    # 重复安装:全有或无一体的回滚已注册名后抛出
    try:
        install(ctx)
        raise AssertionError("expected duplicate-provider error")
    except ValueError:
        pass
    # 回滚保住了首次注册:名字仍在册
    assert "deepseek" in ctx.llm.list_providers()


def test_install_registers_and_resolves():
    asyncio.run(_install_registers_and_resolves())
