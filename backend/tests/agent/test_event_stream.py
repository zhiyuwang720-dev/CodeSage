import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agent.event_stream import RedisAgentEventStream


class _FakeRedisStreamClient:
    def __init__(self):
        self.xadds = []
        self.closed = False

    async def xadd(self, key, fields, maxlen=None, approximate=True):
        self.xadds.append((key, fields, maxlen, approximate))
        return "1-0"

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_redis_event_stream_publishes_json_payload():
    client = _FakeRedisStreamClient()
    stream = RedisAgentEventStream(redis_client=client, key_prefix="agent:events", maxlen=25)

    await stream.publish_event("task-1", {"event_type": "thinking_token", "sequence": 1})

    key, fields, maxlen, approximate = client.xadds[0]
    assert key == "agent:events:task-1"
    assert json.loads(fields["payload"]) == {"event_type": "thinking_token", "sequence": 1}
    assert maxlen == 25
    assert approximate is True
