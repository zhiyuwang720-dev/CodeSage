"""AP04：重试单一所有者与预算（P05）。"""

from __future__ import annotations

import pytest

from .fixture_server import PlannedResponse, RouteScript, openai_completion, openai_stream_chunks

CHAT_PATH = "/v1/chat/completions"
RATE_LIMIT = PlannedResponse(
    status=429,
    payload={"error": {"message": "rate limit exceeded", "type": "rate_limit_error", "code": "rate_limit"}},
    headers={"retry-after": "0"},
)
UNAUTHORIZED = PlannedResponse(
    status=401,
    payload={"error": {"message": "invalid api key", "type": "invalid_request_error", "code": "invalid_api_key"}},
)


@pytest.mark.asyncio
async def test_ap04_harness_stream_issues_one_request_per_attempt(model_harness) -> None:
    """Harness（QueryLoop）拥有预算：一次 stream 只发一次实际请求。"""

    model_harness.server.route(
        CHAT_PATH,
        RouteScript(by_index={0: RATE_LIMIT, 1: PlannedResponse(status=429, payload=RATE_LIMIT.payload)}),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    events = [event async for event in service.chat_completion_stream(
        messages=[{"role": "user", "content": "hi"}], retry_enabled=False
    )]

    assert model_harness.http_count() == 1
    errors = [event for event in events if event["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["error_type"] == "rate_limit"
    assert model_harness.server.requests(CHAT_PATH)[0].body["stream"] is True


@pytest.mark.asyncio
async def test_ap04_independent_stream_uses_bounded_three_attempts(model_harness) -> None:
    """独立调用（SDK 拥有预算）：初次 + 2 次重试，最多 3 次实际请求。"""

    model_harness.server.route(
        CHAT_PATH,
        RouteScript(
            by_index={
                0: RATE_LIMIT,
                1: RATE_LIMIT,
                2: PlannedResponse(sse_chunks=openai_stream_chunks(content="recovered")),
            }
        ),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    events = [event async for event in service.chat_completion_stream(
        messages=[{"role": "user", "content": "hi"}], retry_enabled=True
    )]

    assert model_harness.http_count() == 3
    retries = [event for event in events if event["type"] == "llm_retry"]
    assert [event["attempt"] for event in retries] == [1, 2]
    assert all(event["max_attempts"] == 3 for event in retries)
    done = [event for event in events if event["type"] == "done"]
    assert len(done) == 1
    assert done[0]["content"] == "recovered"

    model_harness.write_json(
        "ap04/retry_budget.json",
        {
            "requirement": "P05",
            "acceptance": "AP04",
            "harness_requests": 1,
            "independent_requests": model_harness.http_count(),
            "retry_events": retries,
        },
    )


@pytest.mark.asyncio
async def test_ap04_auth_error_is_not_retried(model_harness) -> None:
    model_harness.server.route(
        CHAT_PATH,
        RouteScript(by_index={0: UNAUTHORIZED, 1: PlannedResponse(sse_chunks=openai_stream_chunks(content="should not happen"))}),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    events = [event async for event in service.chat_completion_stream(
        messages=[{"role": "user", "content": "hi"}], retry_enabled=True
    )]

    assert model_harness.http_count() == 1
    errors = [event for event in events if event["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["error_type"] == "authentication"


@pytest.mark.asyncio
async def test_ap04_budget_exhaustion_does_not_exceed_three_requests(model_harness) -> None:
    model_harness.server.set_default(CHAT_PATH, RATE_LIMIT)
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    events = [event async for event in service.chat_completion_stream(
        messages=[{"role": "user", "content": "hi"}], retry_enabled=True
    )]

    assert model_harness.http_count() == 3
    assert [event["type"] for event in events][-1] == "error"


@pytest.mark.asyncio
async def test_ap04_non_stream_independent_call_uses_bounded_attempts(model_harness) -> None:
    model_harness.server.route(
        CHAT_PATH,
        RouteScript(
            by_index={
                0: RATE_LIMIT,
                1: PlannedResponse(payload=openai_completion(content="recovered", model="fixture-model")),
            }
        ),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    result = await service.chat_completion(messages=[{"role": "user", "content": "hi"}], purpose="connection_test")

    assert model_harness.http_count() == 2
    assert result["content"] == "recovered"
