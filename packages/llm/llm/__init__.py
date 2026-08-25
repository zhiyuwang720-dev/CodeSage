"""llm 契约包包根:转发 src,保持「import llm」的既有用法。

源码按家族约定住在 src/ 下,src/__init__.py 是真正的导出面;
本文件把公开名连同 types / adapters 子模块转发到包名上 ——
from llm import ...、from llm.types import ...、from llm.adapters.base
import ... 的深引用原样可用,调用方无需关心目录结构。
"""

import sys

from .src import *
from .src import adapters, types

# 深引用(from llm.xxx import)走子模块导入路径,sys.modules 里
# 挂上别名,属性绑定之外 importlib 才能找到
sys.modules[__name__ + ".types"] = types
sys.modules[__name__ + ".adapters"] = adapters

# 根别名:家族目录本身没有包体,顶层 "llm" 可能先被解析成空的
# namespace 壳(谁先 import llm.llm.xxx,壳就固化进 sys.modules);
# 这里把 "llm" 与 "llm.llm" 两个名字都绑定到本模块 —— 无论解析
# 从哪条路径进来(浅引用 from llm import / 深引用 from llm.llm.src
# import),之后都落到同一份模块对象,不存在第二份 src。
import sys as _sys

_mod = _sys.modules[__name__]
_sys.modules["llm"] = _mod
_sys.modules["llm.llm"] = _mod

