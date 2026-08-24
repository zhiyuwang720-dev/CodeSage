"""session —— 会话耐久家族伞包。

DSH 目录结构保留连字符包名(session-persistence /
session-persistence-jsonl);连字符无法在 import 语句里拼出,这里
注册 sys.modules 别名,使消费方可以 dotted 导入:

    from session.session_persistence import PersistenceCoordinator
    from session.session_persistence_jsonl import JsonlSessionPersistence

别名在 import session 时同步注册(惰性包体),先于任何消费方。
"""

import importlib as _importlib
import sys as _sys

_session_persistence = _importlib.import_module("session.session-persistence")
_sys.modules["session.session_persistence"] = _session_persistence

_session_persistence_jsonl = _importlib.import_module("session.session-persistence-jsonl")
_sys.modules["session.session_persistence_jsonl"] = _session_persistence_jsonl
