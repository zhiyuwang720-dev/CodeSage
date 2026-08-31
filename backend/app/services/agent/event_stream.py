from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Optional

from app.core.config import settings


class RedisAgentEventStream:
    def __init__(
        self,
        *,
        redis_client: Optional[Any] = None,
        redis_url: Optional[str] = None,
        key_prefix: str = "agent:events",
        maxlen: Optional[int] = None,
        block_ms: Optional[int] = None,
    ):
        self.redis_client = redis_client
        self.redis_url = redis_url or settings.REDIS_URL
        self.key_prefix = key_prefix
        self.maxlen = maxlen or settings.AGENT_EVENT_STREAM_MAXLEN
        self.block_ms = block_ms or settings.AGENT_EVENT_STREAM_BLOCK_MS
        self._owns_client = redis_client is None

    def key_for_task(self, task_id: str) -> str:
        return f"{self.key_prefix}:{task_id}"

    async def _client(self):
        if self.redis_client is None:
            from redis import asyncio as redis_async

            self.redis_client = redis_async.from_url(self.redis_url, decode_responses=True)
        return self.redis_client

    async def publish_event(self, task_id: str, event_data: dict[str, Any]) -> str:
        client = await self._client()
        payload = json.dumps(event_data, ensure_ascii=False)
        return await client.xadd(
            self.key_for_task(task_id),
            {"payload": payload},
            maxlen=self.maxlen,
            approximate=True,
        )

    async def stream_events(
        self,
        task_id: str,
        *,
        after_sequence: int = 0,
        last_id: str = "0-0",
    ) -> AsyncGenerator[dict[str, Any], None]:
        client = await self._client()
        key = self.key_for_task(task_id)
        current_id = last_id
        while True:
            rows = await client.xread({key: current_id}, count=100, block=self.block_ms)
            if not rows:
                yield {"event_type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()}
                continue
            for _, messages in rows:
                for message_id, fields in messages:
                    current_id = message_id
                    payload = fields.get("payload") if isinstance(fields, dict) else None
                    if not payload:
                        continue
                    event = json.loads(payload)
                    if int(event.get("sequence") or 0) <= after_sequence:
                        continue
                    yield event
                    if event.get("event_type") in {"task_complete", "task_error", "task_cancel"}:
                        return

    async def close(self) -> None:
        if not self._owns_client or self.redis_client is None:
            return
        close = getattr(self.redis_client, "aclose", None)
        if callable(close):
            await close()


def event_stream_enabled() -> bool:
    return bool(settings.AGENT_EVENT_STREAM_ENABLED)


def create_agent_event_stream() -> RedisAgentEventStream:
    return RedisAgentEventStream()
