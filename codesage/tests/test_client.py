"""LLMClient tests: pointer resolution, auxiliary fallback, cost tracking."""

import json

import httpx
import pytest

from codesage.ai import LLMClient, LLMError, LLMRequest, Message
from codesage.config import GlobalConfig, paths


def _cfg(tmp_path, monkeypatch, model_profiles=None, model_pointers=None):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    cfg = GlobalConfig.load()
    if model_profiles is not None:
        cfg.model_profiles = model_profiles
    if model_pointers is not None:
        cfg.model_pointers = model_pointers
    cfg.save()
    return cfg


def _ok_response(text="ok"):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "model": "deepseek-chat",
        },
    )


async def test_pointer_resolution_default_main_is_profile(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    client = LLMClient(project_dir=str(tmp_path))
    profile = client.resolve_profile("main")
    assert profile.model == "main"  # literal fallback: no profiles configured
    await client.aclose()


async def test_pointer_to_profile_chain(tmp_path, monkeypatch):
    _cfg(
        tmp_path,
        monkeypatch,
        model_profiles={
            "main": {"provider": "openai_compatible", "model": "deepseek-chat", "base_url": "https://api.deepseek.com"},
            "fast": {"provider": "openai_compatible", "model": "qwen-plus", "base_url": "https://dashscope.example.com"},
        },
        model_pointers={"main": "main", "task": "fast", "compact": "fast", "quick": "fast"},
    )
    client = LLMClient(project_dir=str(tmp_path))
    assert client.resolve_profile("main").model == "deepseek-chat"
    assert client.resolve_profile("task").model == "qwen-plus"
    assert client.resolve_profile("quick").model == "qwen-plus"
    await client.aclose()


async def test_literal_provider_colon_model(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    client = LLMClient(project_dir=str(tmp_path))
    profile = client.resolve_profile("anthropic:claude-sonnet")
    assert profile.provider == "anthropic" and profile.model == "claude-sonnet"
    await client.aclose()


async def test_auxiliary_failure_falls_back_to_main(tmp_path, monkeypatch):
    _cfg(
        tmp_path,
        monkeypatch,
        model_profiles={
            "main": {"provider": "openai_compatible", "model": "deepseek-chat", "base_url": "https://api.deepseek.com"},
            "fast": {"provider": "openai_compatible", "model": "qwen-plus", "base_url": "https://dashscope.example.com"},
        },
        model_pointers={"main": "main", "task": "fast", "compact": "fast", "quick": "fast"},
    )
    calls = {"n": 0}
    request_bodies = []

    def handler(req):
        body = json.loads(req.content)
        request_bodies.append(body["model"])
        calls["n"] += 1
        if body["model"] == "qwen-plus":
            return httpx.Response(401, text="bad key")
        return _ok_response()

    client = LLMClient(project_dir=str(tmp_path), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    resp = await client.complete(LLMRequest(messages=[Message(role="user", content="hi")]), model="task")
    assert resp.text == "ok"
    assert request_bodies == ["qwen-plus", "deepseek-chat"]  # fell back to main
    await client.aclose()


async def test_main_failure_does_not_fall_back(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    client = LLMClient(
        project_dir=str(tmp_path),
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(500, text="boom"))),
    )
    with pytest.raises(LLMError):
        await client.complete(LLMRequest(messages=[Message(role="user", content="hi")]), model="main")
    await client.aclose()


async def test_retry_happens_on_429(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow")
        return _ok_response()

    client = LLMClient(
        project_dir=str(tmp_path),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    resp = await client.complete(LLMRequest(messages=[Message(role="user", content="hi")]), model="main")
    assert resp.text == "ok"
    assert calls["n"] == 2
    await client.aclose()


async def test_cost_accumulates(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    client = LLMClient(
        project_dir=str(tmp_path),
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda req: _ok_response())),
    )
    assert client.total_cost[0] == 0.0
    await client.complete(LLMRequest(messages=[Message(role="user", content="hi")]))
    # deepseek-chat: 10 * 0.27 + 5 * 1.10 per million
    assert client.total_cost[0] == pytest.approx((10 * 0.27 + 5 * 1.10) / 1_000_000)
    await client.aclose()
