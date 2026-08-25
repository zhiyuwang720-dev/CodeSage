"""core —— 内核家族伞包。

连字符目录(agent-loop)无法在 import 语句里拼出,这里注册
sys.modules 别名,使消费方可以 dotted 导入:

    from core.agent_loop import AgentLoop

顶层连字符伞包(system-prompt)同样无法按名导入 —— 先经
import_module 的字面路径加载,再注册根别名。别名在 import core
时同步注册(惰性包体),先于任何消费方。
"""

import importlib as _importlib
import sys as _sys

_system_prompt = _importlib.import_module("system-prompt")
_sys.modules["system_prompt"] = _system_prompt

_agent_loop = _importlib.import_module("core.agent-loop")
_sys.modules["core.agent_loop"] = _agent_loop
