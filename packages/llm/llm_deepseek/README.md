# llm_deepseek —— OpenAI 兼容协议的提供者包

llm 能力家族的一个提供者:实现 OpenAI Chat Completions 兼容协议
的适配器,一个工厂覆盖多家后端 —— DeepSeek(deepseek)、OpenAI
兼容端点(openai_compatible)、OpenAI(openai)、Qwen(qwen)、
GLM(glm)。各家只差端点与 key 环境变量,协议同构,一个适配器
足矣。

```python
from llm import LLMService
from llm_deepseek import install

ctx.plugin(LLMService)
ctx.plugin(install)  # install.inject = ["llm"],llm 未挂先报错
```

key 环境变量按提供者自动补全:deepseek → DEEPSEEK_API_KEY,
openai 系 → OPENAI_API_KEY,qwen → DASHSCOPE_API_KEY,
glm → ZHIPUAI_API_KEY。

测试:

```bash
cd packages && python -m pytest llm/llm_deepseek/ -q
```
