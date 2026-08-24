"""session/session-persistence-jsonl —— JSONL 耐久会话持久化后端。

每个会话一个追加式日志文件(header 行 + 连续事件行),布局为
``{root}/{projectKey(cwd)}/{encodeSegment(id)}/session.jsonl``。
物化走「temp 写 + fsync + 原子发布」(POSIX link/unlink,Windows
MoveFileExW 写透);崩溃恢复只丢弃撕裂尾、保留已提交前缀,并给
平衡视图合成回合关闭器 —— 事件溯源日志永不重写已提交事件。

与 session-persistence 的关系:本包是后端的实现面(字节文件原语),
协调器在 session-persistence 里(编排面);两者组合出完整的
ctx.sessionPersistence 服务。连字符目录无法在 import 语句里拼出,
消费方经 session 伞包的 sys.modules 别名导入
(session.session_persistence_jsonl)或测试直插 sys.path 后
from src import ...。
"""

# pytest 会把本目录以顶层模块收集(无包上下文),相对导入会崩溃;
# 无包上下文时本文件只作包标记。
if __package__:
    from .src import *  # noqa: F401,F403 -- 公共 API 经 src/__init__.py 收口
