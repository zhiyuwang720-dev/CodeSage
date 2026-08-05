"""LLMClient tests: pointer resolution, auxiliary fallback, cost tracking."""

import asyncio
import json

import httpx
import pytest

from codesage.ai import ContentBlock, LLMClient, LLMError, LLMRequest, Message, StreamEvent
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
            "model": "deepseek-v4-flash",
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
    # deepseek-v4-flash: 10 * 0.14 + 5 * 0.28 per million
    assert client.total_cost[0] == pytest.approx((10 * 0.14 + 5 * 0.28) / 1_000_000)
    await client.aclose()


# ---- production-gap fixes (2026-08-05) ----

async def test_transport_error_wrapped_retryable(tmp_path, monkeypatch):
    """httpx connect failures must become retryable LLMError (network blip)."""
    _cfg(tmp_path, monkeypatch)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused", request=req)
        return _ok_response()

    client = LLMClient(
        project_dir=str(tmp_path),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    resp = await client.complete(LLMRequest(messages=[Message(role="user", content="hi")]))
    assert resp.text == "ok"
    assert calls["n"] == 2  # retried after transport error
    await client.aclose()


async def test_stream_cost_accumulates(tmp_path, monkeypatch):
    """The engine runs on stream(); cost must accumulate there (was always 0)."""
    _cfg(tmp_path, monkeypatch)
    monkeypatch.setenv("CODESAGE_MODEL", "deepseek-v4-flash")

    def handler(req):
        return httpx.Response(
            200,
            text="data: " + json.dumps({"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}) + "\ndata: [DONE]\n",
        )

    client = LLMClient(
        project_dir=str(tmp_path),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    stream = client.stream(LLMRequest(messages=[Message(role="user", content="hi")]))
    resp = await LLMClient.collect(stream)
    assert resp.text == "hi"
    assert client.total_cost[0] == pytest.approx((10 * 0.14 + 5 * 0.28) / 1_000_000)
    await client.aclose()


async def test_stream_retries_once_on_immediate_error(tmp_path, monkeypatch):
    """A stream failing before its first event is retried once."""
    _cfg(tmp_path, monkeypatch)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, text="data: " + json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}) + "\ndata: [DONE]\n")

    client = LLMClient(
        project_dir=str(tmp_path),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    resp = await LLMClient.collect(client.stream(LLMRequest(messages=[Message(role="user", content="hi")])))
    assert resp.text == "ok"
    assert calls["n"] == 2
    await client.aclose()


# ---- A1: cancellation threading ----

async def test_complete_cancelled_before_call(tmp_path, monkeypatch):
    """A pre-set cancel event aborts before any request hits the wire."""
    _cfg(tmp_path, monkeypatch)
    calls = {"n": 0}
    cancel = asyncio.Event()
    cancel.set()
    client = LLMClient(
        project_dir=str(tmp_path),
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda req: (calls.__setitem__("n", calls["n"] + 1), _ok_response())[1])),
        cancel_event=cancel,
    )
    with pytest.raises(LLMError) as exc_info:
        await client.complete(LLMRequest(messages=[Message(role="user", content="hi")]))
    assert exc_info.value.cancelled
    assert not exc_info.value.retryable
    assert calls["n"] == 0
    await client.aclose()


async def test_complete_cancelled_mid_call(tmp_path, monkeypatch):
    """Cancellation during an in-flight request aborts it as LLMError(cancelled)."""
    _cfg(tmp_path, monkeypatch)
    calls = {"n": 0}
    cancel = asyncio.Event()

    async def handler(req):
        calls["n"] += 1
        await asyncio.sleep(30.0)  # never completes unless the abort works
        return _ok_response()

    client = LLMClient(
        project_dir=str(tmp_path),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        cancel_event=cancel,
    )
    task = asyncio.create_task(client.complete(LLMRequest(messages=[Message(role="user", content="hi")])))
    await asyncio.sleep(0.05)
    cancel.set()
    with pytest.raises(LLMError) as exc_info:
        await task
    assert exc_info.value.cancelled
    assert calls["n"] == 1  # aborted, no retry
    await client.aclose()


async def test_cancelled_aux_does_not_fall_back(tmp_path, monkeypatch):
    """A cancelled auxiliary request must not restart against the main profile."""
    _cfg(
        tmp_path,
        monkeypatch,
        model_profiles={
            "main": {"provider": "openai_compatible", "model": "deepseek-chat", "base_url": "https://api.deepseek.com"},
            "fast": {"provider": "openai_compatible", "model": "qwen-plus", "base_url": "https://dashscope.example.com"},
        },
        model_pointers={"main": "main", "task": "fast", "compact": "fast", "quick": "fast"},
    )
    cancel = asyncio.Event()
    models = []

    async def handler(req):
        models.append(json.loads(req.content)["model"])
        await asyncio.sleep(30.0)  # never completes unless the abort works
        return _ok_response()

    client = LLMClient(
        project_dir=str(tmp_path),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        cancel_event=cancel,
    )
    task = asyncio.create_task(client.complete(LLMRequest(messages=[Message(role="user", content="hi")]), model="task"))
    await asyncio.sleep(0.05)
    cancel.set()
    with pytest.raises(LLMError) as exc_info:
        await task
    assert exc_info.value.cancelled
    assert models == ["qwen-plus"]  # no fallback call to main
    await client.aclose()


async def test_stream_cancelled_mid_stream(tmp_path, monkeypatch):
    """Setting cancel between streamed events interrupts collect mid-way."""
    _cfg(tmp_path, monkeypatch)
    cancel = asyncio.Event()
    release = asyncio.Event()
    started = asyncio.Event()

    class SlowAdapter:
        def __init__(self, profile, http):
            self.profile = profile
            self.http = http

        async def acomplete(self, request):
            raise NotImplementedError

        async def astream(self, request):
            yield StreamEvent(type="text_delta", text="hi")
            started.set()
            await release.wait()
            yield StreamEvent(type="done")

    client = LLMClient(project_dir=str(tmp_path), cancel_event=cancel)
    client._adapter = lambda profile: SlowAdapter(profile, None)
    task = asyncio.create_task(
        LLMClient.collect(client.stream(LLMRequest(messages=[Message(role="user", content="hi")])))
    )
    await started.wait()  # first event consumed, stream parked on `release`
    cancel.set()
    release.set()
    with pytest.raises(LLMError) as exc_info:
        await task
    assert exc_info.value.cancelled
    await client.aclose()


# ---- A2: truncated streams / empty streams ----

async def test_collect_drops_tool_use_on_error():
    """An error event mid-stream drops accumulated tool_use (keeps text)."""
    async def events():
        yield StreamEvent(type="text_delta", text="sure")
        yield StreamEvent(type="tool_use_start", tool_use_id="tu1", tool_name="bash")
        yield StreamEvent(type="tool_use_delta", input_json_delta='{"cmd"')
        yield StreamEvent(type="tool_use_delta", input_json_delta=':"ls"}')
        yield StreamEvent(type="error", error="HTTP 503: unavailable")

    resp = await LLMClient.collect(events())
    assert resp.is_error
    assert resp.error_message == "HTTP 503: unavailable"
    assert resp.content == [ContentBlock(type="text", text="sure")]  # no tool_use block
    assert resp.text == "sure"


async def test_stream_empty_raises_retryable(tmp_path, monkeypatch):
    """A stream with zero events is retried once, then raises a retryable error."""
    _cfg(tmp_path, monkeypatch)
    calls = {"n": 0}

    class EmptyAdapter:
        """Adapter whose stream terminates without yielding (real adapters
        always emit at least a done event, so this can't go through MockTransport)."""

        def __init__(self, profile, http):
            self.profile = profile
            self.http = http

        async def acomplete(self, request):
            raise NotImplementedError

        async def astream(self, request):
            calls["n"] += 1
            if False:
                yield None  # keep this an async generator

    client = LLMClient(project_dir=str(tmp_path))
    client._adapter = lambda profile: EmptyAdapter(profile, None)
    with pytest.raises(LLMError) as exc_info:
        await LLMClient.collect(client.stream(LLMRequest(messages=[Message(role="user", content="hi")])))
    assert exc_info.value.retryable
    assert not exc_info.value.cancelled
    assert calls["n"] == 2  # retried once before giving up
    await client.aclose()


# ---- api_key field + startup hint support ----

async def test_profile_api_key_field_preferred(tmp_path, monkeypatch):
    """An explicit api_key on the profile wins over the env var."""
    _cfg(tmp_path, monkeypatch)
    from codesage.ai import ModelProfile

    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    profile = ModelProfile(model="m", api_key="file-key")
    assert profile.get_api_key() == "file-key"


async def test_profile_api_key_falls_back_to_env(tmp_path, monkeypatch):
    from codesage.ai import ModelProfile

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert ModelProfile(model="m").get_api_key() is None
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    assert ModelProfile(model="m").get_api_key() == "env-key"


# ---- PI-03: truncated response drops tool calls ----

async def test_length_truncation_drops_tool_uses(tmp_path, monkeypatch):
    """stop_reason=length with tool_use blocks: blocks dropped, is_error set
    (partial args must never be executed)."""
    _cfg(tmp_path, monkeypatch)

    def handler(req):
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {
                        "content": "thinking out loud",
                        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "rm -'}}],
                    },
                    "finish_reason": "length",
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 100, "total_tokens": 105},
            },
        )

    client = LLMClient(project_dir=str(tmp_path), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    resp = await client.complete(LLMRequest(messages=[Message(role="user", content="hi")]))
    assert resp.is_error
    assert not any(b.type == "tool_use" for b in resp.content)
    assert "truncated" in (resp.error_message or "")
    await client.aclose()
