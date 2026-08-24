"""core/session —— DSH 会话内核(session 包的 Python 移植)。

会话的事件溯源内核:append-only 事件日志是唯一事实源,消息历史、
请求头、todo 视图都是从日志派生的纯函数。本包不依赖任何外部
运行时,可独立测试。
"""

from .src import *  # noqa: F401,F403 -- 公共 API 经 src/__init__.py 收口
