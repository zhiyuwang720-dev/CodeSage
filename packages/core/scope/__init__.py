"""core/scope 包根:转发 src,对齐「import core.scope」的家族约定。

源码与导出面在 src/ 下;本文件把公开名转发到包名上,让
``from core.scope import scope_of, scope_target`` 这类深引用可用。
(session 包的 enter 边界依赖它铸造路由牌。)
"""

from .src import *  # noqa: F401,F403 -- 公共 API 经 src/__init__.py 收口
