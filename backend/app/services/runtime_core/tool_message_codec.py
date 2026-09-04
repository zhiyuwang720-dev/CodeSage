from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from app.services.contracts.models import RuntimeMessageRole, TranscriptItem


class ToolMessageFormat(StrEnum):
    LEGACY_TEXT = "legacy_text"
    OPENAI_TOOLS = "openai_tools"
    ANTHROPIC_BLOCKS = "anthropic_blocks"
    RESPONSES_ITEMS = "responses_items"
    GEMINI_PARTS = "gemini_parts"


def build_runtime_model_messages(
    *,
    system_prompt: str | None,
    recon_payload: dict[str, Any],
    transcript: list[Any],
    tool_definitions: list[dict[str, Any]] | None = None,
    tool_message_format: ToolMessageFormat | str = ToolMessageFormat.OPENAI_TOOLS,
) -> list[dict[str, Any]]:
    """Serialize Runtime transcript items into provider-visible model messages.

    Runtime stores tool events as TranscriptItem(role=TOOL_USE/TOOL_RESULT).
    This serializer preserves their native pairing instead of replaying them as
    plain user text.
    """

    try:
        message_format = ToolMessageFormat(str(tool_message_format))
    except ValueError:
        message_format = ToolMessageFormat.OPENAI_TOOLS

    if message_format is ToolMessageFormat.ANTHROPIC_BLOCKS:
        return _build_anthropic_messages(
            system_prompt=system_prompt,
            recon_payload=recon_payload,
            transcript=transcript,
        )
    if message_format in {ToolMessageFormat.RESPONSES_ITEMS, ToolMessageFormat.GEMINI_PARTS}:
        return _build_openai_messages(
            system_prompt=system_prompt,
            recon_payload=recon_payload,
            transcript=transcript,
            native_tool_history=bool(tool_definitions),
        )
    if message_format is ToolMessageFormat.LEGACY_TEXT:
        return _build_legacy_text_messages(
            system_prompt=system_prompt,
            recon_payload=recon_payload,
            transcript=transcript,
        )
    return _build_openai_messages(
        system_prompt=system_prompt,
        recon_payload=recon_payload,
        transcript=transcript,
        native_tool_history=bool(tool_definitions),
    )


def _system_message(system_prompt: str | None, recon_payload: dict[str, Any]) -> dict[str, Any] | None:
    effective = (system_prompt or "").strip()
    if recon_payload:
        recon_text = "Runtime recon payload:\n" + json.dumps(
            recon_payload, ensure_ascii=False, indent=2
        )
        effective = f"{effective}\n\n{recon_text}".strip() if effective else recon_text
    if not effective:
        return None
    return {"role": "system", "content": effective}


def _item_role(item: Any) -> str:
    return str(getattr(item, "role", "user") or "user")


def _item_content(item: Any) -> str:
    return str(getattr(item, "content", "") or "")


def _item_payload(item: Any) -> dict[str, Any]:
    payload = getattr(item, "payload", {}) or {}
    return dict(payload) if isinstance(payload, dict) else {}


def _item_metadata(item: Any) -> dict[str, Any]:
    metadata = getattr(item, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = getattr(item, "message_metadata", {}) or {}
    return dict(metadata) if isinstance(metadata, dict) else {}


def _tool_use_id(item: Any) -> str:
    payload = _item_payload(item)
    return str(payload.get("tool_use_id") or payload.get("tool_call_id") or "").strip()


def _tool_name(item: Any) -> str:
    payload = _item_payload(item)
    return str(payload.get("tool_name") or getattr(item, "name", None) or "tool")


def _tool_input(item: Any) -> dict[str, Any]:
    payload = _item_payload(item)
    tool_input = payload.get("input")
    return dict(tool_input) if isinstance(tool_input, dict) else {}


def _reasoning_content(item: Any) -> str:
    payload = _item_payload(item)
    metadata = _item_metadata(item)
    return str(
        payload.get("reasoning_content")
        or metadata.get("reasoning_content")
        or payload.get("reasoning")
        or metadata.get("reasoning")
        or ""
    )


def _is_tool_use(item: Any) -> bool:
    return _item_role(item) == RuntimeMessageRole.TOOL_USE.value


def _is_tool_result(item: Any) -> bool:
    return _item_role(item) == RuntimeMessageRole.TOOL_RESULT.value


def _build_openai_messages(
    *,
    system_prompt: str | None,
    recon_payload: dict[str, Any],
    transcript: list[Any],
    native_tool_history: bool,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system = _system_message(system_prompt, recon_payload)
    if system is not None:
        messages.append(system)

    known_tool_use_ids: set[str] = set()

    items = [item for item in transcript if _item_role(item) != RuntimeMessageRole.SYSTEM.value]
    index = 0
    while index < len(items):
        item = items[index]
        role = _item_role(item)
        content = _item_content(item)
        if role == RuntimeMessageRole.ASSISTANT.value:
            assistant: dict[str, Any] = {"role": "assistant", "content": content}
            reasoning_content = _reasoning_content(item)
            if reasoning_content:
                assistant["reasoning_content"] = reasoning_content
            tool_uses, tool_results, next_index = _collect_native_tool_run(items, index + 1)
            tool_calls = _openai_tool_calls(tool_uses, known_tool_use_ids=known_tool_use_ids)
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            if content or reasoning_content or tool_calls:
                messages.append(assistant)
            _append_openai_tool_results(messages, tool_calls, tool_results)
            index = next_index
            continue
        if _is_tool_use(item):
            if not native_tool_history:
                messages.append(
                    {"role": "user", "content": _format_legacy_tool_history(item)}
                )
                index += 1
                continue
            tool_uses, tool_results, next_index = _collect_native_tool_run(items, index)
            tool_calls = _openai_tool_calls(tool_uses, known_tool_use_ids=known_tool_use_ids)
            if tool_calls:
                messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
            _append_openai_tool_results(messages, tool_calls, tool_results)
            index = next_index
            continue
        if _is_tool_result(item):
            if not native_tool_history:
                messages.append(
                    {"role": "user", "content": _format_legacy_tool_result(item)}
                )
            index += 1
            continue
        if role == RuntimeMessageRole.HANDOFF.value:
            target = _item_payload(item).get("target") or "verification"
            messages.append({"role": "user", "content": f"Handoff ({target}):\n{content}"})
        else:
            messages.append({"role": "user", "content": content})
        index += 1

    return messages


def _build_anthropic_messages(
    *,
    system_prompt: str | None,
    recon_payload: dict[str, Any],
    transcript: list[Any],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system = _system_message(system_prompt, recon_payload)
    if system is not None:
        messages.append(system)

    known_tool_use_ids: set[str] = set()

    items = [item for item in transcript if _item_role(item) != RuntimeMessageRole.SYSTEM.value]
    index = 0
    while index < len(items):
        item = items[index]
        role = _item_role(item)
        content = _item_content(item)
        if role == RuntimeMessageRole.ASSISTANT.value:
            blocks: list[dict[str, Any]] = []
            if content:
                blocks.append({"type": "text", "text": content})
            tool_uses, tool_results, next_index = _collect_native_tool_run(items, index + 1)
            tool_use_blocks = _anthropic_tool_use_blocks(tool_uses, known_tool_use_ids=known_tool_use_ids)
            blocks.extend(tool_use_blocks)
            if blocks:
                messages.append({"role": "assistant", "content": blocks})
            _append_anthropic_tool_results(messages, tool_use_blocks, tool_results)
            index = next_index
            continue
        if _is_tool_use(item):
            tool_use_id = _tool_use_id(item)
            if not tool_use_id:
                index += 1
                continue
            tool_uses, tool_results, next_index = _collect_native_tool_run(items, index)
            tool_use_blocks = _anthropic_tool_use_blocks(tool_uses, known_tool_use_ids=known_tool_use_ids)
            if tool_use_blocks:
                messages.append({"role": "assistant", "content": tool_use_blocks})
            _append_anthropic_tool_results(messages, tool_use_blocks, tool_results)
            index = next_index
            continue
        if _is_tool_result(item):
            index += 1
            continue
        if role == RuntimeMessageRole.HANDOFF.value:
            target = _item_payload(item).get("target") or "verification"
            messages.append({"role": "user", "content": f"Handoff ({target}):\n{content}"})
        else:
            messages.append({"role": "user", "content": content})
        index += 1

    return messages


def _collect_native_tool_run(items: list[Any], start_index: int) -> tuple[list[Any], dict[str, Any], int]:
    tool_uses: list[Any] = []
    tool_results: dict[str, Any] = {}
    index = start_index
    while index < len(items) and (_is_tool_use(items[index]) or _is_tool_result(items[index])):
        item = items[index]
        tool_use_id = _tool_use_id(item)
        if _is_tool_use(item):
            tool_uses.append(item)
        elif tool_use_id and tool_use_id not in tool_results:
            tool_results[tool_use_id] = item
        index += 1
    return tool_uses, tool_results, index


def _openai_tool_calls(tool_uses: list[Any], *, known_tool_use_ids: set[str]) -> list[dict[str, Any]]:
    tool_calls: list[dict[str, Any]] = []
    for item in tool_uses:
        tool_use_id = _tool_use_id(item)
        if not tool_use_id or tool_use_id in known_tool_use_ids:
            continue
        known_tool_use_ids.add(tool_use_id)
        tool_calls.append(
            {
                "id": tool_use_id,
                "type": "function",
                "function": {
                    "name": _tool_name(item),
                    "arguments": json.dumps(_tool_input(item), ensure_ascii=False),
                },
            }
        )
    return tool_calls


def _append_openai_tool_results(
    messages: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    tool_results: dict[str, Any],
) -> None:
    for tool_call in tool_calls:
        tool_use_id = str(tool_call.get("id") or "")
        result = tool_results.get(tool_use_id)
        if result is None:
            continue
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_use_id,
                "name": _tool_name(result),
                "content": _item_content(result),
            }
        )


def _anthropic_tool_use_blocks(tool_uses: list[Any], *, known_tool_use_ids: set[str]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for item in tool_uses:
        tool_use_id = _tool_use_id(item)
        if not tool_use_id or tool_use_id in known_tool_use_ids:
            continue
        known_tool_use_ids.add(tool_use_id)
        blocks.append(
            {
                "type": "tool_use",
                "id": tool_use_id,
                "name": _tool_name(item),
                "input": _tool_input(item),
            }
        )
    return blocks


def _append_anthropic_tool_results(
    messages: list[dict[str, Any]],
    tool_use_blocks: list[dict[str, Any]],
    tool_results: dict[str, Any],
) -> None:
    result_blocks: list[dict[str, Any]] = []
    for tool_use in tool_use_blocks:
        tool_use_id = str(tool_use.get("id") or "")
        result = tool_results.get(tool_use_id)
        if result is None:
            continue
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": _item_content(result),
        }
        if _item_metadata(result).get("is_error"):
            block["is_error"] = True
        result_blocks.append(block)
    if result_blocks:
        messages.append({"role": "user", "content": result_blocks})


def _build_legacy_text_messages(
    *,
    system_prompt: str | None,
    recon_payload: dict[str, Any],
    transcript: list[Any],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system = _system_message(system_prompt, recon_payload)
    if system is not None:
        messages.append(system)
    for item in transcript:
        role = _item_role(item)
        content = _item_content(item)
        if role == RuntimeMessageRole.SYSTEM.value:
            continue
        if role == RuntimeMessageRole.ASSISTANT.value:
            messages.append({"role": "assistant", "content": content})
        elif _is_tool_use(item):
            messages.append({"role": "user", "content": _format_legacy_tool_history(item)})
        elif _is_tool_result(item):
            messages.append({"role": "user", "content": _format_legacy_tool_result(item)})
        elif role == RuntimeMessageRole.HANDOFF.value:
            target = _item_payload(item).get("target") or "verification"
            messages.append({"role": "user", "content": f"Handoff ({target}):\n{content}"})
        else:
            messages.append({"role": "user", "content": content})
    return messages


def _format_legacy_tool_history(item: Any) -> str:
    return (
        f"Prior tool request history ({_tool_name(item)}):\n"
        f"{json.dumps(_tool_input(item), ensure_ascii=False)}"
    )


def _format_legacy_tool_result(item: Any) -> str:
    payload = _item_payload(item)
    metadata = _item_metadata(item)
    summary: dict[str, Any] = {
        "tool_name": _tool_name(item),
        "tool_use_id": payload.get("tool_use_id"),
        "tool_call_id": payload.get("tool_call_id"),
        "status": str(metadata.get("status") or ""),
        "is_error": bool(metadata.get("is_error")),
        "content": _item_content(item),
    }
    if isinstance(payload.get("input"), dict):
        summary["input"] = payload["input"]
    if isinstance(payload.get("output"), dict):
        summary["output"] = payload["output"]
    if payload.get("error_message"):
        summary["error_message"] = payload["error_message"]
    return "Tool execution result:\n" + json.dumps(summary, ensure_ascii=False)
