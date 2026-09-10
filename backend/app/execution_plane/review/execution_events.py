"""运行时事件适配：展示故障与执行 Checkpoint 故障严格分离。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.control_plane.results import ReviewResultService, review_result_service

logger = logging.getLogger(__name__)


def build_review_event_sink(
    task_id: str,
    event_manager,
    progress_cb=None,
    task=None,
    db=None,
    db_lock: asyncio.Lock | None = None,
    *,
    result_service: ReviewResultService = review_result_service,
):
    sequence = 0
    lock = db_lock or asyncio.Lock()
    thinking_open: set[str] = set()
    thinking: dict[str, list[str]] = {}
    iterations = 0
    tool_calls = 0
    per_agent: dict[str, dict[str, int]] = {}
    model_requests: dict[str, list[dict[str, Any]]] = {}

    def metadata(perspective: str | None) -> dict:
        return {
            "review_type": "pr_review",
            "perspective": perspective,
            "agent_name": perspective,
        }

    def text(value: Any) -> str:
        if isinstance(value, dict):
            return next(
                (
                    str(value[key]).strip()
                    for key in ("content", "text", "message")
                    if isinstance(value.get(key), str) and value[key].strip()
                ),
                "",
            )
        return str(value or "").strip()

    async def publish(event_type: str, perspective: str | None, message: str, **kwargs) -> None:
        # SSE/Redis/展示写入失败不是业务事实失败。
        try:
            await event_manager.add_event(
                task_id,
                event_type,
                sequence=sequence,
                phase=perspective,
                message=message,
                metadata=kwargs.pop("metadata", metadata(perspective)),
                **kwargs,
            )
        except Exception:
            logger.warning("review display event failed", exc_info=True)

    def usage(raw: dict) -> dict[str, int]:
        input_tokens = int(raw.get("input_tokens") or raw.get("prompt_tokens") or 0)
        output_tokens = int(raw.get("output_tokens") or raw.get("completion_tokens") or 0)
        total = int(raw.get("total_tokens") or input_tokens + output_tokens)
        details = raw.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens") or raw.get("prompt_cache_hit_tokens") or 0)
        return {"total": total, "input": input_tokens, "output": output_tokens, "cached": cached}

    def token_snapshot() -> tuple[int, dict]:
        total = sum(item["total"] for item in per_agent.values())
        total_input = sum(item["input"] for item in per_agent.values())
        total_cached = sum(item["cached"] for item in per_agent.values())
        return total, {
            "per_agent": {
                key: {
                    "latest_total": value["total"],
                    "latest_input": value["input"],
                    "latest_output": value["output"],
                    "cached": value["cached"],
                    "hit_ratio": round(value["cached"] / value["input"], 4)
                    if value["input"] else 0.0,
                }
                for key, value in per_agent.items()
            },
            "total_input": total_input,
            "total_cached": total_cached,
            "cache_hit_ratio": round(total_cached / total_input, 4) if total_input else 0.0,
            "tokens_used": total,
        }

    async def flush_stats() -> None:
        if task is None or db is None:
            return
        total, snapshot = token_snapshot()
        async with lock:
            await result_service.update_runtime_stats(
                db,
                task,
                iterations=iterations,
                tool_calls=tool_calls,
                tokens=total,
                token_stats=snapshot,
            )

    async def close_thinking(perspective: str | None) -> None:
        if perspective and perspective in thinking_open:
            thinking_open.remove(perspective)
            accumulated = "".join(thinking.pop(perspective, []))
            await publish(
                "thinking_end",
                perspective,
                "模型推理完成",
                metadata={"accumulated": accumulated, **metadata(perspective)},
            )

    async def sink(event: dict[str, Any]) -> None:
        nonlocal sequence, iterations, tool_calls
        sequence += 1
        event_type = str(event.get("type") or "message")
        perspective = str(event["perspective"]) if event.get("perspective") else None

        if event_type == "meta":
            suffix = f" #{event['pr_number']}" if event.get("pr_number") is not None else ""
            await publish("review_meta", None, f"PR 审查启动: {event.get('repo') or event.get('project_id') or ''}{suffix}")
        elif event_type == "perspective_start":
            await publish("review_perspective_start", perspective, f"开始 {perspective} 视角审查")
        elif event_type == "session_start" and perspective and event.get("session_id") and db is not None:
            async with lock:
                await result_service.start_perspective(
                    db, task_id, perspective, session_id=str(event["session_id"])
                )
        elif event_type == "assistant_start":
            await publish("assistant_start", perspective, text(event.get("message")) or "模型开始作答")
        elif event_type in ("token", "reasoning_delta"):
            content = text(event.get("content"))
            if not content:
                return
            if perspective and perspective not in thinking_open:
                thinking_open.add(perspective)
                thinking[perspective] = []
                await publish("thinking_start", perspective, "模型开始推理")
            if perspective:
                thinking[perspective].append(content)
            await publish(
                "thinking_token", perspective, content[:200],
                metadata={"token": content, **metadata(perspective)},
            )
        elif event_type == "tool_call":
            tool_calls += 1
            call = event.get("tool_call") or {}
            name = str(call.get("name") or event.get("tool_name") or "?")
            value = call.get("input") or call.get("arguments") or event.get("tool_input")
            await publish(
                "tool_call", perspective, f"调用工具 {name}", tool_name=name,
                tool_input=value if isinstance(value, dict) else None,
            )
        elif event_type == "done":
            await close_thinking(perspective)
            if perspective:
                iterations += 1
            normalized = usage(event.get("usage") or {})
            if perspective:
                model_requests.setdefault(perspective, []).append(
                    {
                        "configured_model": event.get("configured_model"),
                        "request_model": event.get("request_model"),
                        "response_model": event.get("response_model"),
                        "provider": event.get("provider"),
                        "endpoint_id": event.get("endpoint_id"),
                        "protocol": event.get("protocol"),
                        "perspective": perspective,
                        "purpose": event.get("purpose") or "review",
                        "usage": dict(event["usage"]) if event.get("usage") is not None else None,
                    }
                )
            if perspective and normalized["total"]:
                previous = per_agent.get(perspective)
                if not normalized["cached"] and previous and normalized["input"]:
                    normalized["cached"] = min(previous["input"], normalized["input"])
                per_agent[perspective] = normalized
                await publish(
                    "llm_usage", perspective, f"LLM 用量 +{normalized['total']} tokens",
                    tokens_used=normalized["total"], metadata={**metadata(perspective), "usage": dict(event.get("usage") or {})},
                )
            if event.get("task_complete"):
                await flush_stats()
                await publish("task_complete", perspective, text(event.get("message")) or "PR 审查完成")
            else:
                await publish("assistant_done", perspective, text(event.get("message")) or "本轮完成")
        elif event_type == "perspective_done" and perspective:
            raw_findings = event.get("findings")
            if raw_findings is None:
                raw_findings = []
            findings = raw_findings if isinstance(raw_findings, list) else []
            total, _ = token_snapshot()
            # 先提交执行事实；失败向上冒泡。展示事件失败不会反向污染 Checkpoint。
            if db is not None and isinstance(raw_findings, list):
                async with lock:
                    await result_service.complete_perspective(
                        db,
                        task_id,
                        perspective,
                        session_id=str(event["session_id"]) if event.get("session_id") else None,
                        findings=findings,
                        stats={
                            "turn_count": int(event.get("turn_count") or 0),
                            "token_usage": int(per_agent.get(perspective, {}).get("total") or total),
                            "tool_calls": tool_calls,
                            "model_requests": list(model_requests.get(perspective) or []),
                        },
                    )
            await flush_stats()
            if progress_cb is not None:
                try:
                    await progress_cb(perspective)
                except Exception:
                    logger.warning("review progress callback failed", exc_info=True)
            count = len(findings) if isinstance(raw_findings, list) else int(raw_findings or 0)
            await publish("review_perspective_done", perspective, f"完成 {perspective} 视角审查 ({count} 发现)")
        elif event_type == "error":
            await publish("task_error", perspective, text(event.get("message")) or str(event.get("error") or "审查出错"))
        elif event_type == "assistant_tombstone":
            await close_thinking(perspective)
            await publish("assistant_tombstone", perspective, text(event.get("message")) or "模型输出中断")
        elif event_type == "llm_retry":
            await publish("llm_retry", perspective, text(event.get("message")) or "LLM 请求重试")
        elif event_type == "message":
            message = text(event.get("message") or event.get("content"))
            if message:
                await publish("message", perspective, message)

    return sink
