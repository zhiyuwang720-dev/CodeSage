from __future__ import annotations

from app.execution_plane.models.usage import normalize_usage
from app.infrastructure.observability.llm_semantics import llm_span_attributes


def test_a03_openinference_attributes_use_actual_model_and_normalized_usage() -> None:
    usage = normalize_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "total_tokens": 1200,
            "prompt_tokens_details": {"cached_tokens": 400},
        },
        provider="openai",
    )
    assert usage is not None

    attributes = llm_span_attributes(
        configured_model="deepseek-chat",
        request_model="deepseek-chat",
        response_model="deepseek-chat-202609",
        provider="deepseek",
        endpoint_id="https://api.deepseek.com",
        protocol="openai_chat",
        perspective="security",
        purpose="review",
        usage=usage.to_dict(),
    )

    assert attributes["llm.model_name"] == "deepseek-chat"
    assert attributes["llm.model_name"] != "review:security"
    assert attributes["llm.token_count.prompt"] == 1000
    assert attributes["llm.token_count.prompt_details.cache_read"] == 400
    assert attributes["gen_ai.usage.input_tokens"] == 1000
    assert attributes["codesage.perspective"] == "security"


def test_a05_estimated_usage_does_not_populate_standard_billing_fields() -> None:
    usage = normalize_usage(
        None,
        provider="deepseek",
        estimated_input_tokens=10,
        estimated_output_tokens=5,
    )
    # No wire usage means no normalized usage object at all; estimates live on
    # adapter-created missing-usage records, never in standard token attributes.
    assert usage is None
    attributes = llm_span_attributes(
        configured_model="deepseek-chat",
        request_model="deepseek-chat",
        response_model=None,
        provider="deepseek",
        endpoint_id="https://api.deepseek.com",
        protocol="openai_chat",
        perspective="quality",
        purpose="review",
        usage={
            "usage_present": False,
            "field_sources": {"prompt_tokens": "missing", "completion_tokens": "missing"},
            "estimated_usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        },
    )
    assert "llm.token_count.prompt" not in attributes
    assert "gen_ai.usage.input_tokens" not in attributes
