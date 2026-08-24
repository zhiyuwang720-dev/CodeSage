"""llm_replay 提供者包:LLM HTTP 调用的录制与回放(测试/调试用)。

对应 DSH 的 dsh-llm-replay:回放是独立的提供者能力,不在契约层。
与其余提供者包不同,回放不在适配器层做 —— 它挂在传输层(http
客户端上),对任何已注册的提供者适配器都生效:录制的流量按
方法+URL+请求体 指纹落盘,回放时命中指纹直接回放,miss 即报错
(CI 靠它抓漂移)。

与 DSH 的回放适配器差异:DSH 是适配器级的回放(只回放自家协议),
我们选传输层回放 —— 一份录制对 deepseek/anthropic 通吃。
"""

from __future__ import annotations

import httpx

from .vcr import VCRTransport

__all__ = ["VCRTransport", "make_http"]


def make_http(mode: str | None) -> httpx.AsyncClient:
    """构造带回放能力的 http 客户端。

    mode: off(缺省,直连)/ record(转发并落盘)/ replay(只回放)。
    用法:LLMService(ctx, client=LLMClient(http=make_http("replay")))
    —— 回放模式由使用方显式注入,llm 服务不感知。
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0),
        transport=VCRTransport(mode) if mode else None,
    )
