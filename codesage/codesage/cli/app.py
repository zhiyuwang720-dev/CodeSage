"""OpenCode 风格全屏交互 UI(prompt_toolkit Application,阶段 REPL 现代化)。

布局(自上而下):

    [可滚动消息历史区]            启动时含 CODESAGE banner,随对话滚走
    [■ Mode · Model · N.Ns]     仅 turn 进行时出现(working 计时行)
    ┌─────────────────────────┐
    │ Ask anything... "建议"  │  Frame 边框输入框(占位符 + 轮换灰字建议)
    └─────────────────────────┘
    [Mode · Model]              信息行
    [tab 切换模式  ctrl+p 命令]  空闲提示行(turn 期间隐藏)
    [● Tip …]                   轮换提示行(turn 期间隐藏)
    [cwd                N.NK (P%) · $C.CC  ctrl+p commands]  状态栏

交互:/ 开头自动弹命令补全(上下选择、回车采纳并执行、ESC 关闭);turn 进行中
输入不丢失(accept_handler 直接进 steer queue,替代旧 msvcrt 捕获线程);权限
询问改为应用内 y/n/r 按键(enter = 拒绝,与旧默认一致);Ctrl+C 中止运行中
turn,空闲时退出;Ctrl+O 切换 transcript 展开。

repl.py 保留单轮 run_single_turn 与斜杠命令分发;本模块只做交互外壳。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from pathlib import Path

from prompt_toolkit import Application
from prompt_toolkit.application import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.containers import ConditionalContainer, Float, FloatContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame

from ..config import paths
from ..core import user_message
from ..engine import AgentLoop
from ..engine.tokens import DEFAULT_CONTEXT_WINDOW, estimate_context_tokens
from ..permissions import build_rule_string, save_approval
from ..permissions.modes import normalize_mode
from .commands import COMMANDS
from .render import CYAN, GREY, YELLOW, _c, render_message

#: 深色终端样式表
_STYLE = Style.from_dict({
    "input-frame": "bg:#1c1c1c",
    "frame.border": "fg:#555555",
    "frame.label": "fg:#888888",
    "placeholder": "fg:#777777",
    "placeholder.suggestion": "fg:#555555",
    "status": "bg:#101010 fg:#888888",
    "info.mode": "fg:#5f87ff bold",
    "info.model": "fg:#777777",
    "working": "fg:#5f87ff",
    "hints": "fg:#555555",
    "tip": "fg:#af8700",
    "completion-menu": "bg:#2d2d2d",
    "completion-menu.completion": "bg:#2d2d2d fg:#cccccc",
    "completion-menu.completion.current": "bg:#005f87 fg:#ffffff",
    "completion-menu.meta.completion": "bg:#2d2d2d fg:#777777",
    "completion-menu.meta.completion.current": "bg:#005f87 fg:#ffffff",
})

#: banner(双行像素字)
_BANNER = [
    "█▀▀ █▀█ █▀▄ █▀▀ █▀ ▄▀█ █▀▀ █▀▀",
    "█▄▄ █▄█ █▄▀ ██▄ ▄█ █▀█ █▄█ ██▄",
]

#: 占位符轮换建议(灰字引号提示)
_SUGGESTIONS = [
    "Fix broken tests",
    "Explain this codebase",
    "Refactor this module",
    "Add unit tests",
    "Review recent changes",
]

#: 空闲轮换提示
_TIPS = [
    "Commit your project's AGENTS.md file to Git for team sharing",
    "Type / to browse commands and skills",
    "ctrl+o expands collapsed tool output",
    "/mode yolo 自动批准工具调用(谨慎使用)",
]

#: 连续自动继续轮上限(镜像 repl.MAX_AUTO_CONTINUE,后台通知刷屏防无限续跑)
MAX_AUTO_CONTINUE = 5

#: 权限模式 Tab 轮换顺序
_MODE_CYCLE = ("default", "plan", "yolo")


def _mode_label(loop: AgentLoop) -> str:
    """当前权限模式显示名(首字母大写,如 Default/Plan/Yolo)。"""
    return normalize_mode(loop.mode).value.capitalize()


def _model_name(loop: AgentLoop) -> str:
    """模型指针 → 实际模型名(失败回退指针名)。"""
    try:
        client = getattr(loop, "client", None)
        if client is not None:
            return client.resolve_profile(getattr(loop, "model", "main")).model
    except Exception:
        pass
    return getattr(loop, "model", "main")


def _fmt_tokens(tokens: int) -> str:
    """K 缩写:118512 → 118.5K。"""
    return f"{tokens / 1000:.1f}K"


def _status_fragments(loop: AgentLoop, cwd: str, width: int) -> list:
    """状态栏(左 cwd,右 tokens/pct/cost/快捷键提示);宽度填充实现两端对齐。"""
    tokens = estimate_context_tokens(loop._active_messages or []).tokens
    window = DEFAULT_CONTEXT_WINDOW
    pct = int(100 * tokens / window) if window else 0
    cost = 0.0
    client = getattr(loop, "client", None)
    total_cost = getattr(client, "total_cost", None)
    if total_cost:
        try:
            cost = float(total_cost[0])
        except (TypeError, IndexError, ValueError):
            cost = 0.0
    left = f" {cwd}"
    right = f"{_fmt_tokens(tokens)} ({pct}%) · ${cost:.2f}   ctrl+p commands "
    pad = max(1, width - len(left) - len(right))
    return [("class:status", left + " " * pad + right)]


def _working_fragments(loop: AgentLoop, started: float) -> list:
    """turn 进行中计时行:■ Mode · Model · N.Ns。"""
    elapsed = time.monotonic() - started
    return [("class:working", f"■ {_mode_label(loop)} · {_model_name(loop)} · {elapsed:.1f}s")]


def _info_fragments(loop: AgentLoop) -> list:
    """输入框下信息行:Mode · Model。"""
    return [
        ("class:info.mode", _mode_label(loop)),
        ("class:info.model", f" · {_model_name(loop)}"),
    ]


def _suggestion(now: float | None = None) -> str:
    """按时间轮换的占位符建议(约 6s 一条)。"""
    now = time.monotonic() if now is None else now
    return _SUGGESTIONS[int(now // 6) % len(_SUGGESTIONS)]


def _tip(now: float | None = None) -> str:
    """按时间轮换的 Tip(约 15s 一条)。"""
    now = time.monotonic() if now is None else now
    return _TIPS[int(now // 15) % len(_TIPS)]


def _io_for_app() -> dict:
    """非 tty(测试/管道)下用 Dummy IO 构造 Application(Win32 无控制台时
    create_output 会抛 NoConsoleScreenBufferError);tty 下走真实终端。"""
    if sys.stdout.isatty():
        return {}
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput

    return {"output": DummyOutput(), "input": DummyInput()}


class _HistoryLog:
    """消息历史累积器:接收 ANSI 文本(print 的 out 目标),转 formatted
    fragments 供 FormattedTextControl 渲染;每次写入触发 on_change(重绘)。"""

    def __init__(self, on_change) -> None:
        self._fragments: list = []
        self._on_change = on_change

    def write(self, text: str) -> int:
        if text:
            self._fragments.extend(to_formatted_text(ANSI(text)))
            self._on_change()
        return len(text)

    def flush(self) -> None:  # print(..., flush=True) 兼容
        pass

    @property
    def fragments(self) -> list:
        return self._fragments

    def line_count(self) -> int:
        """当前行数(换行计数;滚动钳制用,光标锚到最后一行)。"""
        n = 0
        for _style, text in self._fragments:
            n += text.count("\n")
        return max(0, n)

    def plain_text(self) -> str:
        """测试用:无样式纯文本拼接。"""
        return "".join(t for _s, t, *r in self._fragments)


class _ScrollableHistoryWindow(Window):
    """历史区窗口:鼠标滚轮可回看,默认跟随底部(新内容滚到底)。

    prompt_toolkit 的 ``Window._scroll_when_linewrapping`` 会按光标位置夹紧
    垂直滚动 —— 光标锚在最后一行时每次渲染都把滚动拉回底部,鼠标滚轮往上
    滚瞬间被拽回去(见 fixture: 历史无法鼠标滚动)。这里覆写 ``_scroll`` 取消
    光标夹紧,改由手动管理:跟随状态下滚到底,回看状态保留当前滚动位置,
    滚回底部即恢复跟随。``_scroll_up/_scroll_down``(滚轮)在基类里只改
    vertical_scroll,不会崩(FormattedTextControl 的 move_cursor_* 是 no-op)。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._follow_bottom = True

    def _scroll(self, ui_content, width, height):
        """只夹紧到合法范围;跟随态钉到底,回看态保留用户位置。"""
        max_scroll = max(0, ui_content.line_count - height)
        if self._follow_bottom:
            self.vertical_scroll = max_scroll
        else:
            self.vertical_scroll = max(0, min(self.vertical_scroll, max_scroll))

    def _scroll_up(self):
        super()._scroll_up()
        self._follow_bottom = False  # 滚轮上滚 → 进入回看,不再随新内容跳底

    def _scroll_down(self):
        super()._scroll_down()
        info = self.render_info
        if info is not None and self.vertical_scroll >= info.content_height - info.window_height:
            self._follow_bottom = True  # 滚回底部 → 恢复跟随


class CodeSageApp:
    """全屏交互应用:消息历史 + 边框输入框 + 状态栏,驱动 REPL 循环。"""

    def __init__(
        self,
        loop: AgentLoop,
        *,
        cwd: Path,
        show_thinking: bool = False,
        skills=None,
    ) -> None:
        self.loop = loop
        self.cwd = cwd
        self.state: dict = {
            "show_thinking": show_thinking,
            "loop": loop,
            "transcript": False,
            "skills": skills,
            "_bar_redraw": self._invalidate,
        }
        self._busy = False
        self._turn_started: float | None = None
        self._exit = False
        #: (future, decision, tool, tool_input) —— 非 None 时 y/n/r 键生效
        self._permission: tuple | None = None
        self._log = _HistoryLog(on_change=self._on_log_change)
        self._input_queue: asyncio.Queue[str] = asyncio.Queue()

        self._buffer = Buffer(
            completer=self._make_completer(),
            complete_while_typing=True,
            multiline=False,
            accept_handler=self._on_accept,
        )
        input_control = BufferControl(
            buffer=self._buffer,
            focusable=True,
            input_processors=[
                ConditionalProcessor(
                    AfterInput(
                        lambda: [
                            ("class:placeholder", "Ask anything... "),
                            ("class:placeholder.suggestion", f'"{_suggestion()}"'),
                        ]
                    ),
                    filter=Condition(lambda: self._buffer.text == ""),
                ),
            ],
        )
        self._history_control = FormattedTextControl(
            lambda: self._log.fragments,
            focusable=False,
            show_cursor=False,
            # 光标锚在最后一行:_ScrollableHistoryWindow._scroll 用它之外的机制
            # 管理滚动;此处锚点仅供渲染光标位置(无光标)参考,不驱动滚动。
            get_cursor_position=lambda: Point(x=0, y=self._log.line_count()),
        )
        self._history_window = _ScrollableHistoryWindow(
            self._history_control, wrap_lines=True, dont_extend_height=False
        )
        self._input_window = Window(
            input_control, height=1, dont_extend_height=True
        )
        # 顶层 FloatContainer 包住整个布局:补全弹窗作为 Float 叠在输入光标处
        # (OpenCode 弹窗)。Float 必须挂在覆盖全屏的容器上 —— 若只包 1 行输入框,
        # _draw_float 依据输入框 write_position.height=1 计算菜单高度,空间不足
        # 会收敛到 0,弹窗永远画不出来(见 fixture: '/ 输入无命令列表')。
        # xcursor/ycursor=True 相对输入光标定位;CompletionsMenu 在输入有补全态时
        # 显示(has_focus 恒真——输入是唯一可聚焦控件)。
        body = HSplit([
            self._history_window,
            ConditionalContainer(
                Window(FormattedTextControl(
                    lambda: _working_fragments(self.loop, self._turn_started or 0.0)
                ), height=1, dont_extend_height=True),
                filter=Condition(lambda: self._busy),
            ),
            Frame(self._input_window, style="class:input-frame"),
            Window(FormattedTextControl(
                lambda: _info_fragments(self.loop)
            ), height=1, dont_extend_height=True),
            ConditionalContainer(
                Window(FormattedTextControl(
                    lambda: [("class:hints", "tab 切换模式   ctrl+p 命令   ctrl+o 展开")]
                ), height=1, dont_extend_height=True),
                filter=Condition(lambda: not self._busy),
            ),
            ConditionalContainer(
                Window(FormattedTextControl(
                    lambda: [("class:tip", f"● Tip {_tip()}")]
                ), height=1, dont_extend_height=True),
                filter=Condition(lambda: not self._busy),
            ),
            Window(FormattedTextControl(
                self._status_text
            ), height=1, dont_extend_height=True),
        ])
        layout = Layout(
            FloatContainer(
                body,
                [
                    Float(
                        xcursor=True,
                        ycursor=True,
                        transparent=True,
                        content=CompletionsMenu(
                            max_height=16,
                            scroll_offset=1,
                            extra_filter=has_focus(self._buffer),
                        ),
                    ),
                ],
            ),
            focused_element=self._input_window,
        )
        self._app = Application(
            layout=layout,
            key_bindings=self._build_keybindings(),
            full_screen=True,
            refresh_interval=0.5,  # 工作行计时/Tip 轮换的心跳
            style=_STYLE,
            mouse_support=True,
            **_io_for_app(),
        )

        # 引擎回调接线:权限询问 → 应用内按键;通知 → 历史区
        loop.request_permission = self._request_permission
        loop.on_notification = self._on_notification
        self._banner()

    # ---- 渲染辅助 ----

    def _make_completer(self):
        from .repl import _SlashCompleter  # 函数级:app ← repl 循环引用

        return _SlashCompleter(COMMANDS, self.state.get("skills"))

    def _invalidate(self) -> None:
        """重绘(on_after_render 钩子等调用点)。滚动交给 _ScrollableHistoryWindow:
        跟随态由 _scroll 钉到底,回看态保留用户位置 —— 不在此强写 vertical_scroll
        (强写 10**9 会在回看时把视图拽回底部,破坏鼠标滚轮)。"""
        self._app.invalidate()

    def _on_notification(self, ntype: str, message: str, data) -> None:
        """通知(阶段 09 §2.5):灰字一行进历史区,重绘。"""
        from .repl import _render_notification  # 函数级:app ← repl 循环引用

        self._log.write(_render_notification(ntype, message) + "\n")

    def _on_log_change(self) -> None:
        # 历史区有新内容:滚到底 + 请求重绘(节流交给 prompt_toolkit 渲染帧)
        self._invalidate()

    def _status_text(self) -> list:
        try:
            width = get_app().output.get_size().columns
        except Exception:
            width = 80
        return _status_fragments(self.loop, str(self.cwd), width)

    def _banner(self) -> None:
        """启动 banner:双行像素字写进历史区(随对话滚走)。"""
        self._log.write("\n")
        for line in _BANNER:
            self._log.write(_c(f"  {line}\n", CYAN))
        self._log.write("\n")

    # ---- 键位 ----

    def _build_keybindings(self) -> KeyBindings:
        kb = KeyBindings()
        perm_pending = Condition(lambda: self._permission is not None)

        @kb.add("y", filter=perm_pending)
        @kb.add("n", filter=perm_pending)
        @kb.add("r", filter=perm_pending)
        def _perm_key(event):
            key = event.key_sequence[0].key
            self._resolve_permission(key)

        @kb.add("enter", filter=perm_pending)
        def _perm_enter(event):
            self._resolve_permission("n")  # enter = 拒绝(旧默认一致)

        @kb.add("enter")
        def _enter(event):
            buf = event.app.current_buffer
            if buf.complete_state is not None and buf.complete_state.current_completion:
                buf.apply_completion(buf.complete_state.current_completion)
            buf.validate_and_handle()

        @kb.add("escape")
        def _escape(event):
            buf = event.app.current_buffer
            if buf.complete_state is not None:
                buf.cancel_completion()

        @kb.add("c-o")
        def _ctrl_o(event):
            self.state["transcript"] = not self.state["transcript"]
            self._log.write(
                _c(f"\n[transcript {'on' if self.state['transcript'] else 'off'}]", YELLOW)
            )

        @kb.add("c-c")
        def _ctrl_c(event):
            if self._permission is not None:
                self._resolve_permission("n")
            elif self._busy:
                self.loop.abort.set()  # 中止运行中 turn
            else:
                self._request_exit()

        @kb.add("c-d")
        def _ctrl_d(event):
            if not self._buffer.text:
                self._request_exit()

        @kb.add("c-p")
        def _ctrl_p(event):
            buf = event.app.current_buffer
            if not buf.text:
                buf.text = "/"
                buf.cursor_position = 1
                buf.start_completion(select_first=False)

        @kb.add("tab")
        def _tab(event):
            buf = event.app.current_buffer
            if buf.complete_state is not None:
                buf.complete_next()
            elif not buf.text:
                order = _MODE_CYCLE
                try:
                    cur = normalize_mode(self.loop.mode).value
                    nxt = order[(order.index(cur) + 1) % len(order)]
                except ValueError:
                    nxt = "default"
                self.loop.mode = nxt
                self._log.write(_c(f"\npermission mode -> {nxt}", CYAN))

        return kb

    # ---- 输入与权限 ----

    def _on_accept(self, buf: Buffer) -> bool:
        """回车提交:turn 进行中 → steer queue(引擎中途消费);空闲 → 输入队列。"""
        text = buf.text
        stripped = text.strip()
        if not stripped:
            return False  # keep_text=False → 清空
        # 用户输入回显进历史区(render_message 的用户块样式,视觉分组)
        render_message(user_message(stripped), out=self._log)
        if self._busy and self.loop.steer_queue is not None:
            self.loop.steer_queue.put_nowait(stripped)
        else:
            self._input_queue.put_nowait(stripped)
        return False

    async def _request_permission(self, decision, tool, tool_input) -> bool:
        """引擎权限回调:历史区打横幅,等待 y/n/r 按键(future 解析)。"""
        reason = decision.reason or f"{tool.name} needs approval"
        self._log.write(_c(f"\n[权限请求] {reason}", CYAN))
        self._log.write(_c("\n  (y)es 允许  (n)o 拒绝  (r)emember 记住并允许", GREY))
        fut = asyncio.get_running_loop().create_future()
        self._permission = (fut, tool, tool_input)
        self._invalidate()
        return await fut

    def _resolve_permission(self, key: str) -> None:
        pending = self._permission
        if pending is None:
            return
        self._permission = None
        fut, tool, tool_input = pending
        if key == "r":
            save_approval(paths.local_settings_path(), tool.name,
                          build_rule_string(tool.name, tool_input))
            self._log.write(_c(f"\n已记住:允许 {tool.name}(写入 settings.local.json)", CYAN))
            fut.set_result(True)
        elif key == "y":
            fut.set_result(True)
        else:
            fut.set_result(False)
        self._invalidate()

    # ---- 驱动循环 ----

    async def run(self) -> None:
        """运行全屏应用;驱动协程负责消费输入/自动继续,app 退出后清理。"""
        driver = asyncio.create_task(self._driver())
        try:
            await self._app.run_async()
        finally:
            driver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await driver
            self.loop.cancel_subagents()

    def _request_exit(self) -> None:
        self._exit = True
        self._app.exit()

    async def _driver(self) -> None:
        """REPL 主循环:输入队列 vs 后台通知唤醒,谁先到谁驱动。"""
        auto_continues = 0
        while not self._exit:
            get = asyncio.create_task(self._input_queue.get())
            notify = asyncio.create_task(self.loop._notifications_event.wait())
            done, _pending = await asyncio.wait(
                {get, notify}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in _pending:
                t.cancel()
            if get in done:
                auto_continues = 0
                line = get.result()
                if line.startswith("/"):
                    if await self._handle_slash(line):
                        self._request_exit()
                    continue
                await self._run_turns(line)
            else:
                # 后台通知到达且空闲(输入框为空)→ 自动继续;达限回等待
                self.loop._notifications_event.clear()
                if (auto_continues < MAX_AUTO_CONTINUE
                        and not self._buffer.text and not self._busy):
                    auto_continues += 1
                    await self._auto_continue()

    async def _handle_slash(self, line: str) -> bool:
        """斜杠命令:输出重定向进历史区;返回 True = 退出。"""
        from .repl import _handle_slash_command  # 函数级:app ← repl 循环引用

        with contextlib.redirect_stdout(self._log):
            return await _handle_slash_command(self.loop, line, self.state)

    async def _run_turns(self, line: str) -> None:
        """一轮 + followUp(steer queue 中未消费的中途输入逐条续跑)。"""
        from .repl import _drain_steer_queue  # 函数级:app ← repl 循环引用

        pending: str | None = line
        while pending and not self._exit:
            self.loop.abort.clear()
            # 跨轮失忆修复:loop.history 是构造快照,每轮从会话文件重载
            if self.loop.session is not None:
                self.loop.history = self.loop.session.load()
            await self._one_turn(pending)
            pending = _drain_steer_queue(self.loop)

    async def _one_turn(self, text: str | None) -> None:
        """单轮执行:working 行计时,消息渲染进历史区。"""
        self._busy = True
        self._turn_started = time.monotonic()
        self._invalidate()
        try:
            await self._run_single(text)
        finally:
            self._busy = False
            self._turn_started = None
            self._invalidate()

    async def _run_single(self, text: str | None) -> None:
        from .repl import run_single_turn  # 函数级:app ← repl 循环引用

        await run_single_turn(
            self.loop,
            text,
            show_thinking=self.state["show_thinking"],
            transcript=self.state["transcript"],
            out=self._log,
            on_after_render=self._invalidate,
        )

    async def _auto_continue(self) -> None:
        """空闲自动继续(13 S2):run(None) 消费后台通知;历史重载同用户轮。"""
        self.loop.abort.clear()
        self._log.write(_c("\n[后台任务完成,自动继续…]", GREY))
        if self.loop.session is not None:
            self.loop.history = self.loop.session.load()
        await self._one_turn(None)
