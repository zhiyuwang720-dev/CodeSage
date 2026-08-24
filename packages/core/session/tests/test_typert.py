"""轻量类型注册表测试:登记/查询/强校验/清单。"""

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1]  # 包目录 core/session
sys.path.insert(0, str(_CORE))

import core.session.src.typert as typert  # noqa: E402


def test_define_and_is():
    typert.define("non-empty-str", lambda v: isinstance(v, str) and len(v) > 0)
    assert typert.is_("non-empty-str", "ok")
    assert not typert.is_("non-empty-str", "")
    assert not typert.is_("non-empty-str", 42)
    assert not typert.is_("unknown-type", "anything")  # 未登记 → False


def test_check_raises():
    typert.define("pos-int", lambda v: isinstance(v, int) and v > 0)
    typert.check("pos-int", 5)
    try:
        typert.check("pos-int", -1)
        raise AssertionError("check accepted invalid value")
    except TypeError:
        pass


def test_known_lists_registered():
    typert.define("t-abc", lambda v: v == "abc")
    assert "t-abc" in typert.known()
    assert typert.known() == tuple(sorted(typert.known()))
