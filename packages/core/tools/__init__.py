"""core/tools —— 工具注册表与执行管线契约伞包。

参考实现 把「注册了什么工具、模型怎么看到、调用怎么跑」放一个服务
(ctx.tools)里。批次 2 先实现契约面:调用词表(types.py)、执行
对象/调度器协议/错误码(index.py),注册表本体在后续批次。消费方
(agent-loop 的 tool-calls 调度器)按契约编程,不与实现细节耦合。
"""

from .src import *  # noqa: F401,F403 -- 公共 API 经 src/__init__.py 收口
