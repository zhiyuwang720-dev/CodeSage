# llm_replay —— LLM HTTP 录制与回放(传输层)

llm 能力家族的回放提供者。与其余提供者不同,回放不在适配器层
做 —— 它挂在传输层(http 客户端上),对任何已注册的提供者通吃:
一份录制,deepseek/anthropic 全可回放。DSH 的回放是适配器级
(只回放自家协议),这里刻意选传输层,CI 靠它抓响应漂移。

```python
from llm import LLMClient
from llm_replay import make_http

client = LLMClient(http=make_http("replay"))  # off/record/replay
```

- off:直连,不落盘
- record:转发并落盘(指纹 = 方法+URL+请求体)
- replay:命中指纹回放,miss 报错

测试:

```bash
cd packages && python -m pytest llm/llm_replay/ -q
```
