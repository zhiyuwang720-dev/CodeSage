from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from app.services.llm.types import LLMRequest


UNSUPPORTED_SCHEMA_FIELDS = {"cache_control"}
UNSUPPORTED_SCHEMA_FORMATS = {"uri"}


def _json_loads_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sanitize_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_sanitize_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        if key in UNSUPPORTED_SCHEMA_FIELDS:
            continue
        if key == "format" and item in UNSUPPORTED_SCHEMA_FORMATS:
            continue
        sanitized[key] = _sanitize_schema(item)
    return sanitized


def _function_from_tool(tool: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(tool, dict):
        return None
    if isinstance(tool.get("function"), dict):
        function = deepcopy(tool["function"])
        parameters = function.get("parameters") or {"type": "object", "properties": {}}
        function["parameters"] = _sanitize_schema(parameters)
        return function if function.get("name") else None
    if tool.get("name"):
        return {
            "name": str(tool.get("name") or ""),
            "description": str(tool.get("description") or ""),
            "parameters": _sanitize_schema(tool.get("input_schema") or tool.get("parameters") or {"type": "object", "properties": {}}),
        }
    return None


def _openai_tool(tool: dict[str, Any]) -> dict[str, Any] | None:
    function = _function_from_tool(tool)
    if not function:
        return None
    return {"type": "function", "function": function}


def _openai_tool_call(tool_call: dict[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    name = str(tool_call.get("name") or function.get("name") or "")
    arguments = str(tool_call.get("arguments") or function.get("arguments") or "{}")
    return {
        "id": str(tool_call.get("id") or ""),
        "type": str(tool_call.get("type") or "function"),
        "function": {
            "name": name,
            "arguments": arguments,
        },
    }


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif block.get("type") == "tool_result":
                    parts.append(str(block.get("content") or ""))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part)
    return json.dumps(value, ensure_ascii=False)


def openai_chat_payload(request: LLMRequest, *, model: str, provider: str | None = None) -> dict[str, Any]:
    messages = []
    for message in request.messages:
        data = deepcopy(message.to_dict())
        if data.get("tool_calls"):
            data["tool_calls"] = [_openai_tool_call(tool_call) for tool_call in data.get("tool_calls") or []]
        messages.append(data)
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": bool(request.stream),
    }
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    if request.top_p is not None:
        payload["top_p"] = request.top_p

    tools = [_openai_tool(tool) for tool in request.tools or []]
    tools = [tool for tool in tools if tool]
    if tools:
        payload["tools"] = tools
        if request.parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = request.parallel_tool_calls
    if request.stream and str(provider or "").lower() in {"openai", "deepseek", "mimo", "moonshot", "zhipu", "qwen", "doubao", "minimax"}:
        payload["stream_options"] = {"include_usage": True}
    return payload


def openai_responses_payload(request: LLMRequest, *, model: str) -> dict[str, Any]:
    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in request.messages:
        data = message.to_dict()
        role = str(data.get("role") or "user")
        if role == "system":
            instructions.append(_content_text(data.get("content")))
            continue
        if role == "assistant" and data.get("tool_calls"):
            for tool_call in data.get("tool_calls") or []:
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": str(tool_call.get("id") or ""),
                        "name": str(tool_call.get("name") or (tool_call.get("function") or {}).get("name") or ""),
                        "arguments": str(tool_call.get("arguments") or (tool_call.get("function") or {}).get("arguments") or "{}"),
                    }
                )
            if _content_text(data.get("content")):
                input_items.append({"type": "message", "role": "assistant", "content": _content_text(data.get("content"))})
            continue
        if role == "tool":
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(data.get("tool_call_id") or ""),
                    "output": _content_text(data.get("content")),
                }
            )
            continue
        input_items.append({"type": "message", "role": role if role in {"user", "assistant", "developer"} else "user", "content": _content_text(data.get("content"))})

    tools: list[dict[str, Any]] = []
    for tool in request.tools or []:
        function = _function_from_tool(tool)
        if function:
            tools.append(
                {
                    "type": "function",
                    "name": str(function.get("name") or ""),
                    "description": str(function.get("description") or ""),
                    "parameters": function.get("parameters") or {"type": "object", "properties": {}},
                }
            )

    payload: dict[str, Any] = {"model": model, "input": input_items, "stream": bool(request.stream)}
    if instructions:
        payload["instructions"] = "\n\n".join(part for part in instructions if part)
    if tools:
        payload["tools"] = tools
        if request.parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = request.parallel_tool_calls
    if request.max_tokens is not None:
        payload["max_output_tokens"] = request.max_tokens
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    return payload


def anthropic_messages_payload(request: LLMRequest, *, model: str) -> dict[str, Any]:
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for message in request.messages:
        data = message.to_dict()
        role = str(data.get("role") or "user")
        if role == "system":
            system_parts.append(_content_text(data.get("content")))
            continue
        if role == "tool":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(data.get("tool_call_id") or ""),
                            "content": _content_text(data.get("content")),
                        }
                    ],
                }
            )
            continue
        if role == "assistant" and data.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            text = _content_text(data.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for tool_call in data.get("tool_calls") or []:
                function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(tool_call.get("id") or ""),
                        "name": str(tool_call.get("name") or function.get("name") or ""),
                        "input": _json_loads_object(tool_call.get("arguments") or function.get("arguments")),
                    }
                )
            messages.append({"role": "assistant", "content": blocks})
            continue
        messages.append({"role": role if role in {"user", "assistant"} else "user", "content": _content_text(data.get("content"))})

    tools: list[dict[str, Any]] = []
    for tool in request.tools or []:
        function = _function_from_tool(tool)
        if function:
            tools.append(
                {
                    "name": str(function.get("name") or ""),
                    "description": str(function.get("description") or ""),
                    "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
                }
            )

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": request.max_tokens or 4096,
        "stream": bool(request.stream),
    }
    if system_parts:
        payload["system"] = "\n\n".join(part for part in system_parts if part)
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if tools:
        payload["tools"] = tools
    return payload


def gemini_native_payload(request: LLMRequest, *, model: str) -> dict[str, Any]:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in request.messages:
        data = message.to_dict()
        role = str(data.get("role") or "user")
        if role == "system":
            system_parts.append(_content_text(data.get("content")))
            continue
        if role == "assistant" and data.get("tool_calls"):
            parts: list[dict[str, Any]] = []
            text = _content_text(data.get("content"))
            if text:
                parts.append({"text": text})
            for tool_call in data.get("tool_calls") or []:
                function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                parts.append(
                    {
                        "functionCall": {
                            "id": str(tool_call.get("id") or ""),
                            "name": str(tool_call.get("name") or function.get("name") or ""),
                            "args": _json_loads_object(tool_call.get("arguments") or function.get("arguments")),
                        }
                    }
                )
            contents.append({"role": "model", "parts": parts})
            continue
        if role == "tool":
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "id": str(data.get("tool_call_id") or ""),
                                "name": str(data.get("name") or "tool"),
                                "response": {"content": _content_text(data.get("content"))},
                            }
                        }
                    ],
                }
            )
            continue
        contents.append({"role": "model" if role == "assistant" else "user", "parts": [{"text": _content_text(data.get("content"))}]})

    declarations: list[dict[str, Any]] = []
    for tool in request.tools or []:
        function = _function_from_tool(tool)
        if function:
            declarations.append(
                {
                    "name": str(function.get("name") or ""),
                    "description": str(function.get("description") or ""),
                    "parameters": function.get("parameters") or {"type": "object", "properties": {}},
                }
            )

    payload: dict[str, Any] = {"model": model, "contents": contents}
    if system_parts:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(part for part in system_parts if part)}]}
    if declarations:
        payload["tools"] = [{"functionDeclarations": declarations}]
    generation_config: dict[str, Any] = {}
    if request.temperature is not None:
        generation_config["temperature"] = request.temperature
    if request.max_tokens is not None:
        generation_config["maxOutputTokens"] = request.max_tokens
    if request.top_p is not None:
        generation_config["topP"] = request.top_p
    if generation_config:
        payload["generationConfig"] = generation_config
    return payload
