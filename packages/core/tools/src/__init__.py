"""core/tools 公共 API(参考实现 tools/index.ts 契约面 + types.ts 词表)。"""

from . import types as _types
from .index import *  # noqa: F401,F403 -- 导出清单见 index.py __all__
from .types import *  # noqa: F401,F403 -- 导出清单见 types.py __all__

#: 模块级副作用:types.py 已把 code-dispatch 词表注册进 session
#: 事件表;这里再确认一次保证直连 src.types 的消费方也拿到注册。
_types  # noqa: B018 -- 保持导入副作用显式
