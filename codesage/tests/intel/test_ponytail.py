"""ponytail 融合测试(spec 20 §7:test_ponytail.py)。

覆盖:6 技能注册(幂等)/完整正文组件(7 档阶梯/Intensity/示例/When NOT/Boundaries/输出契约)/
三模式正文过滤(统一头 + 只删非当前档行)/状态机(env>flag>full 优先级、flag 往返、off 删 flag、
停用短语整条匹配)。测试后清理 bundled 单例,避免污染其他模块(全量回归隔离)。
"""

from __future__ import annotations

import pytest

from codesage.intel import PONYTAIL_FULL_BODY, register_ponytail
from codesage.intel.ponytail import (
    PONYTAIL_AUDIT_BODY,
    PONYTAIL_DEBT_BODY,
    PONYTAIL_GAIN_BODY,
    PONYTAIL_HELP_BODY,
    PONYTAIL_REVIEW_BODY,
    PonytailState,
    ponytail_body_for,
)
from codesage.skills.bundled import bundled_skills


@pytest.fixture(autouse=True)
def _clean_bundled_skills():
    """测试后清理 bundled 单例(不影响既有 simplify 等其他模块依赖)。"""
    from codesage.skills.bundled import _bundled_skills

    before = list(_bundled_skills)
    yield
    _bundled_skills[:] = before


# ---------------------------------------------------------------- 技能注册

def test_register_all_six_skills():
    """spec 20 §5.1:6 个技能全部注册(ponytail + 5 兄弟)。"""
    register_ponytail()
    names = {s.name for s in bundled_skills()}
    assert {"ponytail", "ponytail-review", "ponytail-audit",
            "ponytail-debt", "ponytail-gain", "ponytail-help"} <= names


def test_register_idempotent():
    """注册幂等:二次调用不重复添加。"""
    register_ponytail()
    n1 = len(bundled_skills())
    register_ponytail()
    assert len(bundled_skills()) == n1


# ---------------------------------------------------------------- 正文完整

def test_full_body_has_all_sections():
    """① 完整正文:持久/7 档阶梯/规则/输出契约/Intensity 表/示例/When NOT/Boundaries。"""
    body = PONYTAIL_FULL_BODY
    assert "## Persistence" in body
    for rung in ("这需要存在吗", "库内已有", "标准库能做", "原生平台能力覆盖",
                 "已装依赖解决", "能一行", "然后才"):
        assert rung in body, f"阶梯缺档: {rung}"
    assert "## Intensity" in body
    assert "**ultra**" in body
    assert "Example:" in body  # 带引号示例行(过滤机制依赖)
    assert "## When NOT to be lazy" in body
    assert "## Boundaries" in body
    assert "[code] → skipped: [X], add when [Y]." in body  # 输出契约
    assert "ponytail:" in body  # 简化注释标记约定


def test_sibling_bodies_keep_contracts():
    """其他 5 技能:契约原样(review/audit tag 格式、debt 台账、gain 禁编数字)。"""
    assert "L<line>: <tag>" in PONYTAIL_REVIEW_BODY
    assert "net: -<N> lines possible." in PONYTAIL_REVIEW_BODY
    assert "net: -<N> lines, -<M> deps possible." in PONYTAIL_AUDIT_BODY
    assert "grep -rnE '(#|//) ?ponytail:' ." in PONYTAIL_DEBT_BODY
    assert "no-trigger" in PONYTAIL_DEBT_BODY
    assert ("绝不打印仓库级节省数字" in PONYTAIL_GAIN_BODY
            or "NEVER print a per-repo savings" in PONYTAIL_GAIN_BODY)
    assert ("基准中位数" in PONYTAIL_GAIN_BODY or "benchmark median" in PONYTAIL_GAIN_BODY)
    assert "/ponytail-help" in PONYTAIL_HELP_BODY


# ---------------------------------------------------------------- 模式过滤

def test_body_for_off_is_empty():
    """off 模式不注入正文。"""
    assert ponytail_body_for("off") == ""


def test_body_for_unified_header():
    """⑦ 统一头:PONYTAIL MODE ACTIVE — level: {mode}。"""
    for mode in ("lite", "full", "ultra"):
        body = ponytail_body_for(mode)
        assert body.startswith(f"PONYTAIL MODE ACTIVE — level: {mode}\n\n")


def test_body_for_lite_keeps_only_lite():
    """lite:表与示例只保留 lite 档行(对齐参考:非当前档全删)。"""
    body = ponytail_body_for("lite")
    assert "| **lite** |" in body
    assert "- lite:" in body
    assert "| **full** |" not in body and "| **ultra** |" not in body
    assert "- full:" not in body and "- ultra:" not in body


def test_body_for_ultra_keeps_only_ultra():
    """ultra:表与示例只保留 ultra 档行。"""
    body = ponytail_body_for("ultra")
    assert "| **ultra** |" in body
    assert "- ultra:" in body
    assert "| **lite** |" not in body and "| **full** |" not in body
    assert "- lite:" not in body and "- full:" not in body


def test_body_for_full_keeps_only_full():
    """full:表与示例只保留 full 档行(默认档)。"""
    body = ponytail_body_for("full")
    assert "| **full** |" in body
    assert "- full:" in body
    assert "| **lite** |" not in body and "| **ultra** |" not in body
    assert "- lite:" not in body and "- ultra:" not in body


def test_body_for_keeps_main_sections():
    """过滤只动 Intensity 段:阶梯/规则/输出/When NOT/Boundaries 全保留。"""
    body = ponytail_body_for("lite")
    for section in ("## 阶梯", "## 规则", "## 输出", "## When NOT to be lazy", "## Boundaries"):
        assert section in body


def test_body_for_invalid_mode_falls_back_full():
    """非法模式回退 full(异常不外溢)。"""
    assert ponytail_body_for("turbo").startswith("PONYTAIL MODE ACTIVE — level: full")


# ---------------------------------------------------------------- 状态机

def test_state_default_full(tmp_path, monkeypatch):
    """默认 full(无 env/flag)。"""
    monkeypatch.delenv("PONYTAIL_DEFAULT_MODE", raising=False)
    assert PonytailState(tmp_path).mode == "full"


def test_state_env_priority(tmp_path, monkeypatch):
    """env 优先于一切:即使 flag 是 lite,env=ultra 生效。"""
    monkeypatch.setenv("PONYTAIL_DEFAULT_MODE", "ultra")
    (tmp_path / ".ponytail-active").write_text("lite\n", encoding="utf-8")
    assert PonytailState(tmp_path).mode == "ultra"


def test_state_flag_file(tmp_path, monkeypatch):
    """flag 文件次之。"""
    monkeypatch.delenv("PONYTAIL_DEFAULT_MODE", raising=False)
    (tmp_path / ".ponytail-active").write_text("lite\n", encoding="utf-8")
    assert PonytailState(tmp_path).mode == "lite"


def test_state_flag_roundtrip(tmp_path, monkeypatch):
    """set_mode 写 flag → 新实例读 flag 恢复。"""
    monkeypatch.delenv("PONYTAIL_DEFAULT_MODE", raising=False)
    st = PonytailState(tmp_path)
    st.set_mode("ultra")
    assert (tmp_path / ".ponytail-active").read_text(encoding="utf-8").strip() == "ultra"
    assert PonytailState(tmp_path).mode == "ultra"


def test_state_off_deletes_flag(tmp_path, monkeypatch):
    """off 删 flag(下次会话恢复默认)。"""
    monkeypatch.delenv("PONYTAIL_DEFAULT_MODE", raising=False)
    st = PonytailState(tmp_path)
    st.set_mode("lite")
    assert (tmp_path / ".ponytail-active").exists()
    st.set_mode("off")
    assert not (tmp_path / ".ponytail-active").exists()
    assert st.mode == "off"


def test_state_invalid_mode_raises(tmp_path):
    """非法模式抛 ValueError(命令层转换为错误提示)。"""
    with pytest.raises(ValueError):
        PonytailState(tmp_path).set_mode("turbo")


def test_is_off_phrase_exact_match():
    """停用短语整条匹配:不误伤含短语的普通句子。"""
    st = PonytailState()
    assert st.is_off_phrase("stop ponytail") is True
    assert st.is_off_phrase("normal mode") is True
    assert st.is_off_phrase("STOP PONYTAIL") is True  # 大小写不敏感
    assert st.is_off_phrase("stop ponytail please") is False  # 整条匹配
    assert st.is_off_phrase("can we switch to normal mode") is False
    assert st.is_off_phrase("") is False
