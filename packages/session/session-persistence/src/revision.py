"""修订号:后端拥有的不透明持久化修订令牌。

DSH 中修订号是 Branded 字符串类型(TS 的 ``SessionPersistenceRevision``):
一个令牌同时标识「哪个存储源」与「该会话日志的哪一次修订」——
两个独立后端各自的本地计数器即使数值相同也不能相互比较(来源不同)。
读路径拿它做单次读/查往返的稳定性收敛(见协调器的 prepare/load 重试)。

Python 侧用 str 原样承载(无 run-time 品牌),工厂函数保持
DSH 的调用形状:``SessionPersistenceRevision(value)`` 原样返回。
"""

from __future__ import annotations

#: 不透明修订令牌的运行时表示:普通字符串,品牌只在类型注解层。
SessionPersistenceRevision = str


def SessionPersistenceRevision(value: str) -> SessionPersistenceRevision:
    """给后端修订打上持久化修订的身份(运行时是恒等函数)。"""
    return value
