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
