# -*- coding: utf-8 -*-
"""端点冒烟: 验证 paratera OpenAI 兼容端点 + 模型名可用。"""
import asyncio
import os
from pathlib import Path

from openai import AsyncOpenAI


async def main() -> None:
    env = {}
    for line in Path(r"E:\Mac\CodeSage\code-review-benchmark\offline\.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k] = v
    client = AsyncOpenAI(api_key=env["MARTIAN_API_KEY"], base_url=env["MARTIAN_BASE_URL"])
    resp = await client.chat.completions.create(
        model=env["MARTIAN_MODEL"],
        messages=[{"role": "user", "content": "只回答两个字: 在线"}],
        max_tokens=16,
        temperature=0,
    )
    print("模型响应:", resp.choices[0].message.content)
    print("用量:", resp.usage.total_tokens if resp.usage else "n/a")


asyncio.run(main())
