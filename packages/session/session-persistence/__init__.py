"""session/session-persistence —— 持久化抽象契约与协调器。

事件溯源的耐久层:SessionPersistence 定义后端契约(追加式日志 +
header 元数据),PersistenceCoordinator 提供后端无关的编排 ——
每 id 串行化、写合并缓冲、崩溃修复排序、惰性物化与修订号。
具体后端(如 session-persistence-jsonl)实现 PersistenceBackend
原语后组合协调器获得其余一切。
"""

# 连字符目录无法在 import 语句里拼出 —— 消费方经 session 伞包的
# sys.modules 别名导入(session.session_persistence)或测试直插
# sys.path 后 from src import ...。pytest 会把本目录以顶层模块收集
# (无包上下文),相对导入会崩溃;无包上下文时本文件只作包标记。
if __package__:
    from .src import *  # noqa: F401,F403 -- 公共 API 经 src/__init__.py 收口
