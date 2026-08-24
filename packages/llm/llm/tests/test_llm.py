"""llm 包测试:能力接缝 —— 服务挂载、提供者注册/撤销、配置解析。

运行:python -m pytest tests/ -q
前置:llm 包在 packages/ 下,codesage 在仓库根,cordis-py 在仓库根
(独立仓库),本文件自行插入 sys.path,不依赖安装。
"""

import asyncio
import sys
from pathlib import Path

import pytest

_FAMILY = Path(__file__).resolve().parents[2]  # 家族根 packages/llm
_ROOT = Path(__file__).resolve().parents[4]  # 仓库根(CodeSage)
sys.path.insert(0, str(_FAMILY))
sys.path.insert(0, str(_ROOT / "cordis-py"))

from cordis import Context  # noqa: E402

from llm import (  # noqa: E402
    BaseAdapter,
    ContentBlock,
    LLMRequest,
    LLMResponse,
    LLMService,
    LlmCallConfig,
    Message,
    StreamEvent,
    Usage,
    call_config_equals,
    usage_tokens,
)


class _FakeAdapter(BaseAdapter):
    """手搓的最小适配器:不经 http,原样记录请求并回包。

    只实现契约的两个抽象方法,不继承 BaseAdapter 的构造(接缝
    测试不需要真实 http 客户端) —— 鸭子类型足以穿过 LLMClient。
    """

    def __init__(self, profile):
        self.profile = profile
        self.calls: list[LLMRequest] = []

    async def acomplete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        return LLMResponse(
            content=[ContentBlock(type="text", text=f"echo:{self.profile.model}")],
            stop_reason="end",
            usage=Usage(input_tokens=10, output_tokens=5),
            model=self.profile.model,
        )

    async def astream(self, request: LLMRequest):
        self.calls.append(request)
        yield StreamEvent(type="text_delta", text="hi")
        yield StreamEvent(type="usage", usage=Usage(input_tokens=10, output_tokens=5))
        yield StreamEvent(type="done", stop_reason="end")


def make_fake(profile, http=None):
    """适配器工厂:接缝注册的工厂签名(profile, http → 适配器)。"""
    return _FakeAdapter(profile)


def _request() -> LLMRequest:
    return LLMRequest(messages=[Message(role="user", content="hello")])


# --- 服务挂载 ---


async def _mounts_on_ctx():
    ctx = Context()
    service = LLMService(ctx)
    assert service.name == "llm"
    assert ctx.llm is service
    assert ctx.get("llm") is service


def test_service_mounts_on_ctx():
    asyncio.run(_mounts_on_ctx())


async def _list_providers():
    ctx = Context()
    service = LLMService(ctx)
    assert service.list_providers() == []  # 接缝是唯一入口:未注册即不可用
    registration = service.register_provider(["fake"], make_fake)
    assert "fake" in service.list_providers()
    registration.dispose()
    assert "fake" not in service.list_providers()


def test_list_providers_registered_only():
    asyncio.run(_list_providers())


# --- 能力接缝:注册 / 撤销 / 重复 ---


async def _seam_completes():
    ctx = Context()
    service = LLMService(ctx)
    service.register_provider(["fake"], make_fake)
    response = await service.complete(_request(), model="fake:test-model")
    assert response.content[0].type == "text"
    assert response.content[0].text == "echo:test-model"
    assert usage_tokens(response.usage) == 15


def test_provider_factory_seam():
    asyncio.run(_seam_completes())


async def _seam_streams():
    ctx = Context()
    service = LLMService(ctx)
    service.register_provider(["fake"], make_fake)
    events = [ev async for ev in service.stream(_request(), model="fake:test-model")]
    assert [ev.type for ev in events] == ["text_delta", "usage", "done"]


def test_provider_factory_seam_stream():
    asyncio.run(_seam_streams())


async def _dispose_revokes():
    ctx = Context()
    service = LLMService(ctx)
    registration = service.register_provider(["fake"], make_fake)
    await service.complete(_request(), model="fake:test-model")
    registration.dispose()
    registration.dispose()  # 幂等
    assert "fake" not in service.list_providers()
    # 撤销后同名可重新注册(注册表回到空态)
    service.register_provider(["fake"], make_fake)
    assert "fake" in service.list_providers()


def test_dispose_revokes_registration():
    asyncio.run(_dispose_revokes())


async def _duplicate_rejected():
    ctx = Context()
    service = LLMService(ctx)
    service.register_provider(["fake"], make_fake)
    with pytest.raises(ValueError):
        service.register_provider(["fake"], make_fake)
    # 撤销后可重新注册(句柄走通同一撤销路径)
    registration = service.register_provider(["fake2"], make_fake)
    registration.dispose()
    service.register_provider(["fake2"], make_fake)


def test_duplicate_provider_rejected():
    asyncio.run(_duplicate_rejected())


async def _all_or_nothing():
    ctx = Context()
    service = LLMService(ctx)
    service.register_provider(["fake"], make_fake)
    with pytest.raises(ValueError):
        # fake 已占名:整体失败,fake2 也不能半注册
        service.register_provider(["fake", "fake2"], make_fake)
    assert "fake2" not in service.list_providers()
    # 未受影响:fake 仍在册
    assert "fake" in service.list_providers()


def test_all_or_nothing_rollback():
    asyncio.run(_all_or_nothing())


# --- 配置解析 ---


async def _resolve_literal():
    ctx = Context()
    service = LLMService(ctx)
    service.register_provider(["fake"], make_fake)
    config = service.resolve_call_config("fake:test-model")
    assert config == LlmCallConfig(provider="fake", model="test-model")
    assert call_config_equals(config, LlmCallConfig(provider="fake", model="test-model"))
    assert not call_config_equals(config, LlmCallConfig(provider="fake", model="other"))
    assert not call_config_equals(config, "not-a-config")


def test_resolve_call_config_literal():
    asyncio.run(_resolve_literal())


def test_call_config_defaults_equal_none():
    # 显式 None 与缺省等价
    a = LlmCallConfig(provider="fake", model="m", temperature=None)
    b = LlmCallConfig(provider="fake", model="m")
    assert call_config_equals(a, b)


async def _cancel_idempotent():
    ctx = Context()
    service = LLMService(ctx)
    service.cancel()
    service.cancel()  # 幂等:重复取消不抛错


def test_cancel_is_idempotent():
    asyncio.run(_cancel_idempotent())
