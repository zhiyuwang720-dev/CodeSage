from __future__ import annotations

import pytest

from app.execution_plane.models.service import LLMService
from app.execution_plane.models.types import LLMResponse
from app.execution_plane.models.usage import normalize_usage
from app.execution_plane.runtime.bridge import RuntimeLLMModelClient


class _IdentityAdapter:
    async def complete(self, request):
        usage = normalize_usage(
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            provider="deepseek",
        )
        return LLMResponse(
            content="done",
            model="deepseek-chat-202609",
            usage=usage,
            finish_reason="stop",
        )


@pytest.mark.asyncio
async def test_a03_identity_and_usage_survive_adapter_service_bridge(monkeypatch) -> None:
    service = LLMService(
        user_config={
            "llmConfig": {
                "llmProvider": "deepseek",
                "llmApiKey": "test-key",
                "llmModel": "deepseek-chat",
                "llmBaseUrl": "https://user:secret@api.deepseek.com/v1?api_key=secret",
                "endpointProtocol": "openai_chat",
            }
        }
    )
    monkeypatch.setattr(
        "app.execution_plane.models.service.LLMFactory.create_adapter",
        lambda config: _IdentityAdapter(),
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
    assert response.endpoint_id == "https://api.deepseek.com"
    assert response.protocol == "openai_chat"
    assert response.perspective == "security"
    assert response.purpose == "review"
    assert response.usage is not None
    assert response.usage["total_tokens"] == 15
    assert response.usage["field_sources"]["total_tokens"] == "provider"
