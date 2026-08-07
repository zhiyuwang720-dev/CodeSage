"""S2 匹配与配置解析测试(§9.1 test_registry.py 子集):matcher 三级 / 配置解析丢弃与警告 / 快照。

三层合并语义由 settings 层测试承担(settings.py:46-56 identity 去重),此处测解析本身。
"""

import logging

from codesage.hooks._common import HookGroup, match_matcher, parse_hook_config


# ---------------------------------------------------------------------------
# matcher 三级匹配(§2.3)


def test_matcher_exact():
    """纯工具名 → 精确比较。"""
    assert match_matcher("Bash", "Bash")
    assert not match_matcher("Bash", "Read")
    assert not match_matcher("Bash", "Bash2")


def test_matcher_pipe_or():
    """`|` 管道 = OR,逐段精确比较(§2.3 第 1 级)。"""
    assert match_matcher("Bash|Write", "Bash")
    assert match_matcher("Bash|Write", "Write")
    assert not match_matcher("Bash|Write", "Read")


def test_matcher_regex():
    """含其他字符 → 正则(search 语义,§2.3 第 2 级)。"""
    assert match_matcher("^(Bash|Write)$", "Bash")
    assert match_matcher("[Bb]ash", "bash")
    assert match_matcher("Bash$", "XBash")  # search 非 match
    assert not match_matcher("^Read$", "ReadFile")
    assert match_matcher("Bash.*", "BashExtra")


def test_matcher_invalid_regex_never_matches(caplog):
    """非法正则 → 永不匹配 + warning(§2.3/§8.2,CC 同款)。"""
    with caplog.at_level(logging.WARNING, logger="codesage.hooks"):
        assert match_matcher("[", "Bash") is False
        assert match_matcher("**", "Bash") is False  # 裸 `*` 才匹配所有,`**` 是非法正则
    assert sum("invalid matcher regex" in r.message for r in caplog.records) == 2


def test_matcher_empty_or_star_matches_all():
    """空 / None / `*` → 匹配所有(§2.3 第 3 级)。"""
    for matcher in (None, "", "*"):
        assert match_matcher(matcher, "Bash")
        assert match_matcher(matcher, "anything")


# ---------------------------------------------------------------------------
# settings.hooks 解析(§3.1/§3.2)


def test_parse_basic_event_group_hooks():
    """事件 → matcher 组 → 单钩子列表;matcher 默认 None(匹配所有),组顺序保留。"""
    parsed = parse_hook_config(
        {
            "PreToolUse": [
                {"matcher": "Bash|Write", "hooks": [{"type": "command", "command": "a.sh"}]},
                {"hooks": [{"type": "command", "command": "b.sh"}]},
            ],
            "SessionStart": [{"hooks": [{"type": "prompt", "prompt": "ctx:$ARGUMENTS"}]}],
        }
    )
    groups = parsed["PreToolUse"]
    assert [g.matcher for g in groups] == ["Bash|Write", None]
    assert [g.hooks[0].command for g in groups] == ["a.sh", "b.sh"]
    assert parsed["SessionStart"][0].hooks[0].prompt == "ctx:$ARGUMENTS"


def test_parse_group_order_within_group():
    """同组多钩子按配置顺序(执行顺序 = 合并后数组顺序,§3.2)。"""
    parsed = parse_hook_config(
        {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": "first"},
                        {"type": "command", "command": "second"},
                    ],
                }
            ]
        }
    )
    assert [h.command for h in parsed["PreToolUse"][0].hooks] == ["first", "second"]


def test_parse_hooks_not_dict_no_hooks(caplog):
    """settings.hooks 非 dict → error 日志,视为无钩子(§3.1)。"""
    with caplog.at_level(logging.ERROR, logger="codesage.hooks"):
        assert parse_hook_config("nope") == {}
        assert parse_hook_config(None) == {}
    assert sum("must be an object" in r.message for r in caplog.records) == 2


def test_parse_unknown_event_discarded():
    """未知事件名 → 丢弃 + warning(§3.1)。"""
    warnings: list[str] = []
    parsed = parse_hook_config(
        {"Nope": [{"hooks": [{"type": "command", "command": "x"}]}]}, warn=warnings.append
    )
    assert parsed == {}
    assert any("unknown hook event 'Nope'" in w for w in warnings)


def test_parse_invalid_group_shapes_discarded():
    """组不是 dict / hooks 不是 list / matcher 类型错 → 丢弃该组 + warning。"""
    warnings: list[str] = []
    parsed = parse_hook_config(
        {
            "PreToolUse": [
                "not-a-group",
                {"hooks": [{"type": "command", "command": "ok"}]},
                {"hooks": "not-a-list"},
                {"matcher": 123, "hooks": [{"type": "command", "command": "x"}]},
            ]
        },
        warn=warnings.append,
    )
    assert [g.hooks[0].command for g in parsed["PreToolUse"]] == ["ok"]
    assert any("must be an object" in w for w in warnings)
    assert any("requires a 'hooks' list" in w for w in warnings)
    assert any("matcher for event 'PreToolUse' must be a string" in w for w in warnings)


def test_parse_invalid_hook_entry_discarded():
    """单钩子字段非法 → 该条丢弃 + warning(经 HookSpec,§3.1)。"""
    warnings: list[str] = []
    parsed = parse_hook_config(
        {
            "PreToolUse": [
                {
                    "hooks": [
                        {"type": "command", "command": "ok"},
                        {"type": "agent", "command": "bad"},  # 未知 type
                    ]
                }
            ]
        },
        warn=warnings.append,
    )
    assert [h.command for h in parsed["PreToolUse"][0].hooks] == ["ok"]
    assert any("unknown hook type 'agent'" in w for w in warnings)


def test_parse_empty_groups_dropped():
    """空组/空事件 → 无钩子,结果不含该事件。"""
    parsed = parse_hook_config({"SessionStart": [], "PreToolUse": [{"hooks": []}]})
    assert parsed == {}


def test_parse_matcher_ignored_event_warns_not_dropped():
    """UserPromptSubmit/Stop 带 matcher → warning 不丢弃(§2.3)。"""
    warnings: list[str] = []
    parsed = parse_hook_config(
        {
            "UserPromptSubmit": [
                {"matcher": "x", "hooks": [{"type": "command", "command": "c"}]}
            ]
        },
        warn=warnings.append,
    )
    assert any("matcher is ignored for event 'UserPromptSubmit'" in w for w in warnings)
    assert parsed["UserPromptSubmit"][0].matcher == "x"
    # 空 matcher 不警告
    warnings = []
    parse_hook_config(
        {"Stop": [{"hooks": [{"type": "command", "command": "c"}]}]}, warn=warnings.append
    )
    assert warnings == []


def test_parse_http_url_whitelist():
    """http 钩子 URL 白名单(§4.9):默认 [] 全禁;`*` 通配 / 精确 / 前缀通配放行。"""
    cfg = {"PreToolUse": [{"hooks": [{"type": "http", "url": "http://127.0.0.1:8000/g"}]}]}
    warnings: list[str] = []
    parsed = parse_hook_config(cfg, warn=warnings.append)
    assert parsed == {}  # 默认全禁 → 钩子丢弃
    assert any("whitelist" in w for w in warnings)

    parsed = parse_hook_config(cfg, http_hook_urls=["*"])
    assert parsed["PreToolUse"][0].hooks[0].url == "http://127.0.0.1:8000/g"

    parsed = parse_hook_config(cfg, http_hook_urls=["http://127.0.0.1:8000/*"])
    assert len(parsed["PreToolUse"][0].hooks) == 1

    warnings = []
    parse_hook_config(cfg, http_hook_urls=["http://elsewhere/"], warn=warnings.append)
    assert any("whitelist" in w for w in warnings)


def test_parse_non_evaluable_event_if_passthrough():
    """非可求值事件带 if → warning 不丢弃,if_evaluable=False(§2.4,永不执行)。"""
    warnings: list[str] = []
    parsed = parse_hook_config(
        {
            "Stop": [
                {"hooks": [{"type": "command", "command": "c", "if": "Bash(git *)"}]}
            ]
        },
        warn=warnings.append,
    )
    assert any("cannot evaluate 'if'" in w for w in warnings)
    spec = parsed["Stop"][0].hooks[0]
    assert spec.if_ == "Bash(git *)"
    assert spec.if_evaluable is False


def test_parse_all_events_keyed():
    """八事件均可作为配置键(§2.2);Notification 经同一解析路径。"""
    cfg = {
        event: [{"hooks": [{"type": "command", "command": "c"}]}]
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
                      "Stop", "PreCompact", "PostCompact", "Notification")
    }
    parsed = parse_hook_config(cfg)
    assert set(parsed) == set(cfg)


# ---------------------------------------------------------------------------
# 快照语义(§3.2)


def test_snapshot_after_parse():
    """解析一次后,修改 settings 源 dict 不生效(快照冻结,会话中配置不热载)。"""
    cfg = {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}
        ]
    }
    parsed = parse_hook_config(cfg)
    # 模拟会话中 settings.json 被改写(§3.2:build_loop 解析一次后不重读)
    cfg["PreToolUse"][0]["matcher"] = "Read"
    cfg["PreToolUse"][0]["hooks"][0]["command"] = "other.sh"
    group = parsed["PreToolUse"][0]
    assert group.matcher == "Bash"
    assert group.hooks[0].command == "guard.sh"


def test_parse_returns_hook_groups():
    """HookGroup 结构:event/matcher/hooks 三字段。"""
    parsed = parse_hook_config(
        {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}]}
    )
    group: HookGroup = parsed["PreToolUse"][0]
    assert group.event == "PreToolUse"
    assert group.matcher == "Bash"
    assert group.hooks[0].command == "x"
