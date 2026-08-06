"""Status bar tests: text layout, ctx meter thresholds, tty gating."""

import io
import time

from codesage.cli.statusbar import CRITICAL_FRACTION, StatusBar, _git_branch


def _bar(meter=None, **kw) -> StatusBar:
    bar = StatusBar(model_name="deepseek-v4-flash", out=io.StringIO(), **kw)
    if meter is not None:
        bar._meter = meter
    return bar


def test_render_text_contains_all_sections():
    bar = _bar(meter=lambda: 0)
    text = bar.render_text()
    assert "branch:" in text
    assert "Model: deepseek-v4-flash" in text
    assert "session:0m" in text
    assert "ctx:[" in text and "0%" in text


def test_ctx_meter_fills_with_tokens():
    bar = _bar(meter=lambda: 64_000)  # half of the 128K default window
    text = bar.render_text()
    assert "ctx:[#####-----]50%" in text


def test_ctx_meter_critical_above_threshold(monkeypatch):
    monkeypatch.setattr("codesage.cli.render.USE_COLOR", True)
    bar = _bar(meter=lambda: 128_000)  # full 128K default window
    text = bar.render_text()
    assert "ctx:[##########]100%" in text
    assert "CRITICAL" in text  # 100% > 90%
    assert "\033[31m" in text  # red


def test_ctx_meter_warns_above_80(monkeypatch):
    monkeypatch.setattr("codesage.cli.render.USE_COLOR", True)
    bar = _bar(meter=lambda: int(128_000 * CRITICAL_FRACTION) - 1_000)
    text = bar.render_text()
    assert "CRITICAL" not in text
    assert "\033[33m" in text  # yellow warning band


def test_ctx_meter_plain_when_low(monkeypatch):
    monkeypatch.setattr("codesage.cli.render.USE_COLOR", False)
    bar = _bar(meter=lambda: 1_000)
    text = bar.render_text()
    assert "CRITICAL" not in text
    assert "\033[" not in text  # no color at all


def test_thinking_flag_shown_when_on():
    bar = _bar(meter=lambda: 0)
    bar.thinking_on = True
    assert "thinking" in bar.render_text()


def test_enable_is_inert_off_tty():
    """Non-tty streams (tests, single-shot) never emit ANSI or scroll regions."""
    out = io.StringIO()
    bar = StatusBar(model_name="m", out=out)
    bar.enable()
    assert bar.enabled is False
    assert out.getvalue() == ""


def test_git_branch_probe():
    assert _git_branch() != ""  # repo-agnostic: runs anywhere, never raises
    assert _git_branch("definitely-not-a-dir-xyz") == "-"


def test_disable_restores_scroll_region_only_when_enabled():
    out = io.StringIO()
    bar = StatusBar(model_name="m", out=out)
    bar.disable()  # no-op when never enabled
    assert out.getvalue() == ""


# ---- ANSI layout sequences (fake tty) ----

class FakeTTY(io.StringIO):
    """A tty-shaped stream so enable()/redraw() emit their control sequences."""

    def isatty(self):
        return True


def test_layout_sequences(monkeypatch):
    """The interactive layout contract, pinned on the emitted bytes:
    scroll region 1..rows-2; redraw parks the cursor back at the region
    bottom (review R3); input line and bar row stay below the region."""
    import os
    import shutil

    from codesage.cli import statusbar as sb_mod

    # enable() gates on USE_COLOR — bound at import time, so patch the
    # statusbar namespace, not the render module's
    monkeypatch.setattr(sb_mod, "USE_COLOR", True)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda: os.terminal_size((80, 24)))
    out = FakeTTY()
    bar = StatusBar(model_name="m", out=out)

    bar.enable()
    s = out.getvalue()
    assert "\033[2J" in s  # clear screen
    assert "\033[1;22r" in s  # scroll region = lines 1..rows-2 (22)
    out.seek(0); out.truncate()

    bar.redraw()
    s = out.getvalue()
    assert "\033[24;1H" in s  # bar painted on row 24
    assert s.endswith("\033[22;1H")  # cursor parked at the region bottom
    out.seek(0); out.truncate()

    bar.move_to_input()
    assert out.getvalue() == "\033[23;1H"  # input line row 23
    out.seek(0); out.truncate()

    bar.print_below("note")
    assert "\033[22;1H" in out.getvalue()  # notes land in the region
    out.seek(0); out.truncate()

    bar.disable()
    assert "\033[r" in out.getvalue()  # scroll region restored


def test_after_submit_clears_input_and_enters_region(monkeypatch):
    """Enter at the prompt: the echoed input is erased and the cursor moves
    into the scroll region so the reply renders above (not on the bar)."""
    import os
    import shutil

    from codesage.cli import statusbar as sb_mod

    monkeypatch.setattr(sb_mod, "USE_COLOR", True)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda: os.terminal_size((80, 24)))
    out = FakeTTY()
    bar = StatusBar(model_name="m", out=out)
    bar.enable()
    out.seek(0)
    out.truncate()

    bar.after_submit()
    s = out.getvalue()
    assert "\033[23;1H" in s  # input line row 23
    assert "\033[2K" in s  # submitted text erased
    assert "\033[22;1H" in s  # cursor re-enters the scroll region (row 22)
    assert "\n" in s  # and moves down a line for the reply
