"""core/agent —— 参考实现 agent 服务(session 内核之上的活体运行时层)。

注册表跟踪活 agent,initiator 作用域携带发起 agent 穿过一条
进程内异步驱动链;agent 拥有的待办消息(收件箱)每次变更都以
耐久事件入会话日志,活体通知与投影只是派生视图。具体创建与
驱动属于 agent-loop 包。
"""

from .src import *  # noqa: F401,F403 -- 公共 API 经 src/__init__.py 收口
