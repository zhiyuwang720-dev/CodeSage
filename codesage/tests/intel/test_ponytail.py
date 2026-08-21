"""ponytail 融合测试(spec 20 §7:test_ponytail.py)。

覆盖:内置技能注册(经 14)/阶梯建议生成(不同影响面)。
测试后清理 bundled 单例,避免污染其他模块的 bundled 技能断言(全量回归隔离)。
"""

import pytest

from codesage.intel import PONYTAIL_BODY, ladder_suggestion, register_ponytail
from codesage.skills.bundled import _clear_bundled_skills, bundled_skills


@pytest.fixture(autouse=True)
def _clean_bundled_skills():
    """测试后清理 bundled 单例(不影响既有 simplify 等其他模块依赖)。"""
    from codesage.skills.bundled import _bundled_skills

    before = list(_bundled_skills)
    yield
    _bundled_skills[:] = before


def test_register_ponytail_skill():
    """spec 20 §5.1:ponytail 作为内置技能注册(经 14 技能系统)。"""
    _clear_bundled_skills()
    register_ponytail()
    names = [s.name for s in bundled_skills()]
    assert "ponytail" in names


def test_ponytail_body_contains_ladder():
    """spec 20 §5.1:技能正文含懒人阶梯核心。"""
    assert "YAGNI" in PONYTAIL_BODY
    assert "标准库" in PONYTAIL_BODY or "stdlib" in PONYTAIL_BODY.lower()
    assert "根因" in PONYTAIL_BODY


def test_ladder_suggestion_no_callers():
    """spec 20 §5.2:无入站调用 → YAGNI 提示。"""
    s = ladder_suggestion(0)
    assert "YAGNI" in s


def test_ladder_suggestion_single_caller():
    """spec 20 §5.2:单调用者 → 复用/根因提示。"""
    s = ladder_suggestion(1)
    assert "复用" in s or "根因" in s


def test_ladder_suggestion_many_callers():
    """spec 20 §5.2:多调用者 → 共享函数/最小改动提示。"""
    s = ladder_suggestion(5)
    assert "5" in s
    assert "共享函数" in s or "复用" in s