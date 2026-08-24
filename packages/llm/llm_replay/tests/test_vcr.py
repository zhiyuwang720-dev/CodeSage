"""VCR record/replay tests (transport level, fully offline)."""

import sys
from pathlib import Path

_FAMILY = Path(__file__).resolve().parents[2]  # 家族根 packages/llm
_ROOT = Path(__file__).resolve().parents[4]  # 仓库根(CodeSage)
sys.path.insert(0, str(_FAMILY))
sys.path.insert(0, str(_ROOT / "cordis-py"))

import json

import httpx
import pytest

from llm_replay import VCRTransport


def _recorded_client(vcr_dir, mode="record"):
    inner = httpx.MockTransport(lambda req: httpx.Response(200, json={"echo": req.url.path}))
    return httpx.AsyncClient(
        transport=VCRTransport(mode, vcr_dir, inner=inner), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_record_then_replay(tmp_path):
    client = _recorded_client(tmp_path, "record")
    r1 = await client.post("/chat/completions", json={"m": 1})
    assert r1.json()["echo"] == "/chat/completions"
    await client.aclose()
    fixtures = list(tmp_path.glob("*.json"))
    assert len(fixtures) == 1

    replay = _recorded_client(tmp_path, "replay")
    r2 = await replay.post("/chat/completions", json={"m": 1})
    assert r2.json() == r1.json()
    await replay.aclose()


@pytest.mark.asyncio
async def test_replay_miss_raises(tmp_path):
    client = _recorded_client(tmp_path, "replay")
    with pytest.raises(RuntimeError, match="replay miss"):
        await client.post("/chat/completions", json={"m": 1})
    await client.aclose()


@pytest.mark.asyncio
async def test_same_fingerprint_same_fixture(tmp_path):
    client = _recorded_client(tmp_path, "record")
    await client.post("/a", json={"x": 1})
    await client.post("/b", json={"x": 1})  # different path -> different fixture
    await client.aclose()
    assert len(list(tmp_path.glob("*.json"))) == 2
