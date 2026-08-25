"""core/agent-loop —— 具体 agent 循环插件伞包。

包名保留连字符(agent-loop):目录名与包身份一致,便于对照外部
参考;连字符无法在 import 语句里拼出,这里注册 sys.modules
别名,使消费方可以 dotted 导入:

    from agent_loop.agent_loop.src.index import AgentLoop
    from core.agent_loop import AgentLoop

别名在 import agent_loop 时同步注册(惰性包体),先于任何消费方。
"""

import importlib as _importlib
import sys as _sys

_agent_loop = _importlib.import_module("core.agent-loop.agent-loop")
# 根模块 + 子模块双别名:import 系统先解析 "agent_loop" 根,缺根会
# ModuleNotFoundError;两个键都指向同一个包体,from 任意路径都得到
# 同一份模块对象(与 core/agent 伞包同模式)。
_sys.modules["agent_loop"] = _agent_loop
_sys.modules["agent_loop.agent_loop"] = _agent_loop

# 公共面转发:连字符包内的相对导入(from .src)在嵌套父包下不可
# 靠,这里用字面路径显式提符号 —— 消费方 from core.agent_loop
# import X 与 from core.agent_loop.src import X 拿到同一符号。
_src = _importlib.import_module("core.agent-loop.agent-loop.src")
for _name in getattr(_src, "__all__", ()):
    globals()[_name] = getattr(_src, _name)
