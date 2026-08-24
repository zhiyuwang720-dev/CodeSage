# llm_anthropic —— Anthropic Messages API 提供者包

llm 能力家族的一个提供者:实现 Anthropic Messages API 的适配器,
注册提供者名 anthropic。与 llm_deepseek 同形态 —— 一个包、一个
注册入口,协议在包内自足,契约层不感知任何一家。

```python
from llm import LLMService
from llm_anthropic import install

ctx.plugin(LLMService)
install(ctx)  # 注册 anthropic
```

key 环境变量自动补全为 ANTHROPIC_API_KEY。

测试:

```bash
cd packages && python -m pytest llm/llm_anthropic/ -q
```
