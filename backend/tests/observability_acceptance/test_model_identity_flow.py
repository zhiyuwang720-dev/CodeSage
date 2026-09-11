"""身份与 usage 端到端贯通：本地端点 → SDK client → service → bridge（P02/P07）。

证据来自真实请求 + 真实响应，不 mock SDK 发送。
"""

from __future__ import annotations

import pytest

from app.execution_plane.runtime.bridge import RuntimeLLMModelClient

from .fixture_server import PlannedResponse, openai_completion


@pytest.mark.asyncio
async def test_identity_and_usage_survive_sdk_service_bridge(model_harness) -> None:
    model_harness.server.set_default(
        "/v1/chat/completions",
        PlannedResponse(
            payload=openai_completion(
                content="done",
                model="deepseek-chat-202609",
                prompt_tokens=11,
                completion_tokens=4,
            )
        ),
    )
    service = model_harness.service(
        provider="deepseek",
        model="deepseek-chat",
        extra={"llmBaseUrl": model_harness.server.base_url},
    )
    client = RuntimeLLMModelClient(llm_service=service, agent_type="review:security")

    response = await client.complete(
        system_prompt="review",
        recon_payload={},
        transcript=[],
        model_name="review:security",
        tool_definitions=[],
    )

    assert response.configured_model == "deepseek-chat"
    assert response.request_model == "deepseek-chat"
    assert response.response_model == "deepseek-chat-202609"
    assert response.provider == "deepseek"
    assert response.endpoint_id == f"http://127.0.0.1:{model_harness.server.port}"
    assert response.protocol == "openai_chat"
    assert response.perspective == "security"
    assert response.purpose == "review"
    assert response.usage is not None
    assert response.usage["total_tokens"] == 15
    assert response.usage["usage_source"] == "sdk_normalized"
    assert response.usage["field_sources"]["total_tokens"] == "sdk_normalized"

    records = model_harness.server.requests("/v1/chat/completions")
    assert len(records) == 1
    assert records[0].body["model"] == "deepseek-chat"
