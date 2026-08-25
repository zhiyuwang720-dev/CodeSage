"""system-prompt —— 参考实现 提示装配注册表实现。

有序系统分节、动态上下文、工具 schema 与提示变量按作用域链注册,
``assemble()`` 每次模型步骤前收集装配,瀑布可改写权威结果。
"""

from .src import *  # noqa: F401,F403 -- 公共 API 经 src/__init__.py 收口
