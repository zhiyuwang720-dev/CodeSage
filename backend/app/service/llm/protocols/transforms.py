"""Protocol-specific request payload transforms."""

from typing import Any, Dict, List

from ..types import LLMRequest


def _messages_to_openai_format(messages: list) -> List[Dict[str, Any]]:
    """Convert LLMMessage list to OpenAI-compatible dict list."""
    return [m.to_dict() for m in messages]


def openai_chat_payload(request: LLMRequest, model: str) -> Dict[str, Any]:
    """Build an OpenAI Chat Completions API payload."""
    payload: Dict[str, Any] = {
        "model": model,
        "messages": _messages_to_openai_format(request.messages),
    }
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    if request.tools:
        payload["tools"] = request.tools
    if request.stream:
        payload["stream"] = request.stream
    return payload


def openai_responses_payload(request: LLMRequest, model: str) -> Dict[str, Any]:
    """Build an OpenAI Responses API payload.

    Maps a chat-style LLMRequest onto the Responses endpoint.
    """
    messages = _messages_to_openai_format(request.messages)
    input_parts: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            input_parts.append({"role": "system", "content": [{"type": "input_text", "text": str(content)}]})
        elif role == "user":
            input_parts.append({"role": "user", "content": [{"type": "input_text", "text": str(content)}]})
        elif role == "assistant":
            input_parts.append({"role": "assistant", "content": [{"type": "output_text", "text": str(content)}]})

    payload: Dict[str, Any] = {
        "model": model,
        "input": input_parts,
    }
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    if request.tools:
        payload["tools"] = request.tools
    return payload
