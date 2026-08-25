"""system-prompt —— 提示装配注册表伞包。

参考实现 目录结构保留连字符包名(system-prompt);连字符无法在 import
语句里拼出,这里注册 sys.modules 别名,使消费方可以 dotted 导入:

    from system_prompt.system_prompt.src.index import SystemPrompt

别名在 import system_prompt 时同步注册(惰性包体),先于任何消费方。
"""

import importlib as _importlib
import sys as _sys

_system_prompt = _importlib.import_module("system-prompt.system-prompt")
# 根 + 子双别名:import 系统先解析 "system_prompt" 根,缺根会
# ModuleNotFoundError;两个键都指向同一个包体,from 任意路径都得到
# 同一份模块对象。
_sys.modules["system_prompt"] = _system_prompt
_sys.modules["system_prompt.system_prompt"] = _system_prompt
