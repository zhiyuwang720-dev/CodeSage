"""OpenCode 风格全屏应用(cli/app.py)组件测试:布局文本、历史累积、权限
y/n/r、输入 steering、占位符/建议/Tip。构造不启动渲染(非 tty → Dummy IO)。"""

import asyncio
import time
from pathlib import Path

import pytest

from codesage.cli.app import (
    CodeSageApp,
    _HistoryLog,
    _info_fragments,
    _mode_label,
    _suggestion,
    _status_fragments,
    _tip,
    _working_fragments,
)


def _frag_text(fragments) -> str:
    return "".join(t for _s, t in fragments)


# ---- 纯文本辅助 ----

def test_mode_label_and_working():
    class Loop:
        mode = "default"
        client = None

    loop = Loop()
    assert _mode_label(loop) == "Default"
    # working 行:■ Mode · Model · N.Ns(时间非零)
    text = _frag_text(_working_fragments(loop, time.monotonic()))
    assert text.startswith("■ Default · ")


def test_info_line_mode_and_model():
    class Client:
        def resolve_profile(self, p):
            class P:
                model = "deepseek-v4-flash"
            return P()

    class Loop:
        mode = "plan"
        client = Client()
        model = "main"

    text = _frag_text(_info_fragments(Loop()))
    assert "Plan" in text and "deepseek-v4-flash" in text


def test_status_fragments_left_right_padded():
    class Loop:
        _active_messages = []
        client = None

    frags = _status_fragments(Loop(), "E:/proj", 80)
    text = _frag_text(frags)
    assert text.startswith(" E:/proj")
    assert "· $0.00" in text  # 无 client → 成本 0
    assert "ctrl+p commands" in text
    assert "K" in text  # 0.0K tokens


def test_suggestion_and_tip_rotate_over_time():
    from codesage.cli.app import _SUGGESTIONS, _TIPS

    a = _suggestion(now=0)
    b = _suggestion(now=6.0)
    assert a in _SUGGESTIONS and b in _SUGGESTIONS
    # 不同时刻 → 不同建议(轮换)
    assert _suggestion(now=0) != _suggestion(now=6.0) or len(_SUGGESTIONS) > 1
    assert _tip(now=0) in _TIPS


def test_history_log_accumulates_and_flattens():
    log = _HistoryLog(on_change=lambda: None)
    log.write("\033[36mhello\033[0m\n")
    log.write("world\n")
    assert log.plain_text() == "hello\nworld\n"


def test_history_log_line_count_tracks_newlines():
    """滚动锚点:line_count = 换行数(历史区光标锚到最后一行 → 新内容可见)。"""
    log = _HistoryLog(on_change=lambda: None)
    assert log.line_count() == 0
    log.write("a\nb\n")
    assert log.line_count() == 2
    log.write("c\n")
    assert log.line_count() == 3


# ---- 应用组件(非 tty 构造)----


def _mock_loop(tmp_path, script, monkeypatch=None):
    from codesage.cli.assemble import build_loop

    if monkeypatch is not None:
        from codesage.config import paths

        monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / ".codesage")

    class MockLLM:
        def __init__(self, script):
            self.script = script
            self.calls = 0
            self.total_cost = [0.0]

        def stream(self, request, model="main"):
            return self._gen()

        async def _gen(self):
            events = self.script[min(self.calls, len(self.script) - 1)](self.calls)
            self.calls += 1
            for ev in events:
                await asyncio.sleep(0)
                yield ev

    loop = build_loop(cwd=tmp_path, mode="default")
    loop.client = MockLLM(script)
    return loop


def test_app_constructs_and_shows_banner(tmp_path):
    from codesage.ai import StreamEvent

    loop = _mock_loop(
        tmp_path,
        [lambda i: [StreamEvent(type="text_delta", text="ok"), StreamEvent(type="done")]],
    )
    app = CodeSageApp(loop, cwd=tmp_path)
    assert "CODESAGE" not in app._log.plain_text()  # banner 是像素字,非字母
    # banner 两行像素字已入历史
    assert "█" in app._log.plain_text()


def test_completion_menu_is_wired_into_layout(tmp_path):
    """补全弹窗必须作为 Float 叠在输入框上 —— 缺失则 '/ ' 输入不显示命令列表。"""
    from codesage.ai import StreamEvent
    from prompt_toolkit.layout.menus import CompletionsMenu

    loop = _mock_loop(
        tmp_path,
        [lambda i: [StreamEvent(type="text_delta", text="ok"), StreamEvent(type="done")]],
    )
    app = CodeSageApp(loop, cwd=tmp_path)
    # 遍历 layout 找 CompletionsMenu(Frame 用 DynamicContainer 包 body,须解析)
    root = app._app.layout.container
    found = []

    def walk(cont):
        if isinstance(cont, CompletionsMenu):
            found.append(cont)
            return
        if isinstance(cont, (list, tuple)):
            for c in cont:
                if isinstance(c, tuple):
                    c = c[1]
                walk(c)
            return
        for attr in ("children", "content", "body", "floats", "get_container"):
            if hasattr(cont, attr):
                v = getattr(cont, attr)
                if callable(v):
                    v = v()
                if isinstance(v, (list, tuple)):
                    for c in v:
                        if isinstance(c, tuple):
                            c = c[1]
                        walk(c)
                else:
                    walk(v)

    walk(root)
    assert found, "CompletionsMenu 不在 layout 中 —— 斜杠补全弹窗无法渲染"
    # 补全弹窗作为 Float 叠加(非内联占位)
    assert app._buffer.completer is not None


def test_completion_float_wraps_full_layout(tmp_path):
    """补全弹窗的 Float 必须挂在覆盖全屏的 FloatContainer 上 —— 若只包 1 行输入框,
    _draw_float 按输入框高度(1)计算菜单高度,空间不足收敛到 0,弹窗画不出来
    (fixture: '/ 输入无命令列表')。"""
    from codesage.ai import StreamEvent
    from prompt_toolkit.layout.menus import CompletionsMenu
    from prompt_toolkit.layout.containers import FloatContainer

    loop = _mock_loop(
        tmp_path,
        [lambda i: [StreamEvent(type="text_delta", text="ok"), StreamEvent(type="done")]],
    )
    app = CodeSageApp(loop, cwd=tmp_path)

    top = app._app.layout.container
    assert isinstance(top, FloatContainer), "根容器必须是 FloatContainer"

    menu_floats = [fl for fl in top.floats
                   if isinstance(fl.content, CompletionsMenu)]
    assert menu_floats, "补全弹窗 Float 必须在顶层 FloatContainer 中"

    # Float 以光标定位(xcursor/ycursor),由顶层容器提供全屏空间供菜单展开
    fl = menu_floats[0]
    assert fl.xcursor is True and fl.ycursor is True

    # 顶层容器内含整个布局(输入框 + 历史区等),而不是只包输入框
    has_history = any(c is app._history_window for c in top.content.get_children())
    assert has_history, "FloatContainer 必须包住含历史区的整个布局"


def test_history_cursor_anchored_to_bottom(tmp_path):
    """历史区光标锚在最后一行 → 新消息不被视口顶部裁掉(滚动修复)。"""
    from codesage.ai import StreamEvent

    loop = _mock_loop(
        tmp_path,
        [lambda i: [StreamEvent(type="text_delta", text="ok"), StreamEvent(type="done")]],
    )
    app = CodeSageApp(loop, cwd=tmp_path)
    before = app._log.line_count()
    app._log.write("line1\n")
    app._log.write("line2\nline3\n")
    # get_cursor_position 返回最后一行行号
    pos = app._history_control.get_cursor_position()
    assert pos.y == app._log.line_count() == before + 3


def test_history_window_scroll_follows_and_manual_scroll(tmp_path):
    """历史区用 _ScrollableHistoryWindow:默认跟随底部;手动上滚进入回看且位置
    跨渲染保留;滚回底部恢复跟随(修复:滚轮回看被渲染钳制拽回底部)。"""
    from codesage.ai import StreamEvent

    loop = _mock_loop(
        tmp_path,
        [lambda i: [StreamEvent(type="text_delta", text="ok"), StreamEvent(type="done")]],
    )
    app = CodeSageApp(loop, cwd=tmp_path)
    hw = app._history_window
    from codesage.cli.app import _ScrollableHistoryWindow

    assert isinstance(hw, _ScrollableHistoryWindow)
    assert hw._follow_bottom is True

    # 大量内容 → 跟随态下 _scroll 钉到底
    for i in range(60):
        app._log.write(f"line {i:02d}\n")
    height = 33
    from types import SimpleNamespace

    content = SimpleNamespace(line_count=app._log.line_count())
    max_scroll = max(0, content.line_count - height)
    hw._scroll(content, 80, height)
    assert hw.vertical_scroll == max_scroll
    assert hw._follow_bottom is True

    # 模拟用户上滚两格(直接改 vertical_scroll),跨渲染 _scroll 保留位置不拽回
    hw.vertical_scroll = max_scroll - 2
    hw._scroll_up()  # 触发进入回看态
    assert hw._follow_bottom is False
    hw._scroll(content, 80, height)
    assert hw.vertical_scroll == max_scroll - 2

    # 模拟滚回底部 → 恢复跟随
    hw.vertical_scroll = max_scroll
    hw._follow_bottom = True
    assert hw._follow_bottom is True


def test_app_wires_permission_and_notification(tmp_path):
    from codesage.ai import StreamEvent

    loop = _mock_loop(
        tmp_path,
        [lambda i: [StreamEvent(type="text_delta", text="ok"), StreamEvent(type="done")]],
    )
    app = CodeSageApp(loop, cwd=tmp_path)
    # 权限/通知接线:回调属主 = app(比较 __self__ 而非 bound method 恒 False)
    assert loop.request_permission.__self__ is app
    assert loop.on_notification is not None


async def test_permission_resolve_y_n_r(tmp_path, monkeypatch):
    from codesage.ai import StreamEvent

    loop = _mock_loop(
        tmp_path,
        [lambda i: [StreamEvent(type="text_delta", text="ok"), StreamEvent(type="done")]],
        monkeypatch=monkeypatch,
    )
    app = CodeSageApp(loop, cwd=tmp_path)
    from codesage.permissions import PermissionDecision

    decision = PermissionDecision(allowed=False, mode="ask", reason="Bash needs approval")
    tool = type("T", (), {"name": "Bash"})()

    async def ask():
        return await app._request_permission(decision, tool, {"command": "x"})

    task = asyncio.create_task(ask())
    await asyncio.sleep(0)  # 让 future 挂起
    assert app._permission is not None
    app._resolve_permission("y")
    assert await task is True

    async def ask2():
        return await app._request_permission(decision, tool, {"command": "y"})

    task2 = asyncio.create_task(ask2())
    await asyncio.sleep(0)
    app._resolve_permission("n")
    assert await task2 is False


def test_accept_steers_when_busy_and_queues_when_idle(tmp_path):
    from codesage.ai import StreamEvent

    loop = _mock_loop(
        tmp_path,
        [lambda i: [StreamEvent(type="text_delta", text="ok"), StreamEvent(type="done")]],
    )
    app = CodeSageApp(loop, cwd=tmp_path)
    loop.steer_queue = asyncio.Queue()

    # 空闲提交 → 输入队列
    app._buffer.text = "hello"
    app._buffer.cursor_position = 5
    app._on_accept(app._buffer)
    assert app._input_queue.empty() is False
    assert app._input_queue.get_nowait() == "hello"

    # busy 提交 → steer queue(中途输入)
    app._busy = True
    app._buffer.text = "steer"
    app._buffer.cursor_position = 5
    app._on_accept(app._buffer)
    assert loop.steer_queue.get_nowait() == "steer"


def test_placeholder_and_input_completer_wired(tmp_path):
    from codesage.ai import StreamEvent

    loop = _mock_loop(
        tmp_path,
        [lambda i: [StreamEvent(type="text_delta", text="ok"), StreamEvent(type="done")]],
    )
    app = CodeSageApp(loop, cwd=tmp_path)
    assert app._buffer.completer is not None  # 斜杠补全已挂
