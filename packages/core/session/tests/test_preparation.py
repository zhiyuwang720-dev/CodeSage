"""准备期所有权测试:dispose 同步幂等、release 恰一次。

照 DSH preparation.spec.ts 的核心断言面。
"""

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1]  # 包目录 core/session
sys.path.insert(0, str(_CORE))

from core.session.src.preparation import SessionPreparation  # noqa: E402


class _FakeSession:
    pass


def test_create_wraps_session_and_release():
    released = []
    session = _FakeSession()
    prep = SessionPreparation.create(session, {"release": lambda: released.append(1)})
    assert prep.session is session
    assert released == []
    prep.dispose()
    assert released == [1]


def test_dispose_is_idempotent():
    released = []
    prep = SessionPreparation.create(_FakeSession(), {"release": lambda: released.append(1)})
    prep.dispose()
    prep.dispose()
    prep.dispose()
    assert released == [1]


def test_release_optional():
    prep = SessionPreparation.create(_FakeSession())
    prep.dispose()  # 不抛
    prep.dispose()
