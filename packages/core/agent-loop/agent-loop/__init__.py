"""agent-loop —— agent 循环驱动。

ReactLoopAgent 驱动一个会话走过回合与步骤边界:每次请求都从
会话日志派生(消息历史、请求头、运行时上下文快照),工具调用按
并发模式分群调度,取消与中止都沉淀成耐久事件。
"""

from .src import *  # noqa: F401,F403 -- 公共 API 经 src/__init__.py 收口
