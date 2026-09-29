"""`sombra ui-demo` and the fake driver behind it."""

import argparse
import asyncio
import io
import sys
import threading
from typing import Any

import aiohttp
import pytest

from sombra.cli import build_parser
from sombra.contracts import ActionKind, UserAction
from sombra.ui import OverlayUI, commands, demo, window
from sombra.ui.system import no_clipboard, no_notify


async def test_drive_runs_the_script() -> None:
    async with OverlayUI(notifier=no_notify, clipboard=no_clipboard) as ui:
        await demo.drive(ui, interval=0, think=0, rounds=len(demo.SCRIPT))
        kinds = [c.kind.value for c in ui.state.cards()]
    assert kinds == ["failure", "suggestion", "suggestion"]


async def test_report_actions() -> None:
    lines: list[str] = []
    async with OverlayUI(notifier=no_notify, clipboard=no_clipboard) as ui:
        task = asyncio.create_task(demo.report_actions(ui, lines.append))
        ui._actions.put_nowait(UserAction("s1", ActionKind.EDIT, "nova"))
        await asyncio.sleep(0.01)
    await asyncio.wait_for(task, timeout=2)
    assert lines == ["s1: edit 'nova'\n"]
    assert demo.format_action(UserAction("s2", ActionKind.DISCARD)) == "s2: discard\n"


def test_ui_demo_is_registered() -> None:
    args = build_parser().parse_args(["ui-demo", "--rounds", "2", "--no-open"])
    assert args.func is commands._run
    assert args.rounds == 2


def _args(**kw: Any) -> argparse.Namespace:
    base = {
        "interval": 0.0,
        "think": 0.0,
        "rounds": 1,
        "browser": False,
        "no_open": False,
        "no_clipboard": True,
    }
    return argparse.Namespace(**{**base, **kw})


def test_run_with_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(window, "window_available", lambda: True)

    def fake_window(url: str) -> None:
        opened.append(url)

        async def check() -> None:
            async with aiohttp.ClientSession() as s, s.get(url) as r:
                assert r.status == 200

        asyncio.run(check())

    monkeypatch.setattr(window, "open_window", fake_window)
    assert commands._run(_args()) == 0
    assert len(opened) == 1
    assert f"overlay at {opened[0]}" in capsys.readouterr().out


def test_run_in_browser_until_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(window, "window_available", lambda: False)
    monkeypatch.setattr(window, "open_in_browser", opened.append)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    real_join = threading.Thread.join
    calls = {"n": 0}

    def join(self: threading.Thread, timeout: float | None = None) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt  # the user pressed Ctrl-C
        real_join(self, timeout)

    monkeypatch.setattr(threading.Thread, "join", join)
    assert commands._run(_args()) == 0
    assert len(opened) == 1
    assert "pywebview not installed" in sys.stderr.getvalue()  # type: ignore[attr-defined]


def test_run_reports_bind_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(self: OverlayUI) -> None:
        raise OSError("address in use")

    monkeypatch.setattr(OverlayUI, "start", fail)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert commands._run(_args(no_open=True)) == 1
    assert "address in use" in sys.stderr.getvalue()  # type: ignore[attr-defined]


def test_run_reports_any_startup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError

    monkeypatch.setattr(OverlayUI, "__init__", broken)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert commands._run(_args(no_open=True)) == 1
    assert "RuntimeError" in sys.stderr.getvalue()  # type: ignore[attr-defined]


def test_window_helpers_without_pywebview(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> None:
        raise ImportError(name)

    monkeypatch.setattr(window.importlib, "import_module", missing)
    assert window.window_available() is False
    with pytest.raises(ImportError):
        window.open_window("http://127.0.0.1:1/")
    opened: list[str] = []
    monkeypatch.setattr(window.webbrowser, "open", lambda url: opened.append(url) or True)
    assert window.open_in_browser("http://127.0.0.1:1/") is True
    assert opened == ["http://127.0.0.1:1/"]


def test_open_window_with_fake_webview(monkeypatch: pytest.MonkeyPatch) -> None:
    class Event(list[Any]):
        def __iadd__(self, handler: Any) -> "Event":  # type: ignore[override]
            self.append(handler)
            return self

    class Events:
        def __init__(self) -> None:
            self.shown = Event()

    class FakeWindow:
        def __init__(self) -> None:
            self.events = Events()

    class FakeWebview:
        def __init__(self) -> None:
            self.created: list[tuple[Any, ...]] = []
            self.started = False
            self.window = FakeWindow()

        def create_window(self, *args: Any, **kw: Any) -> FakeWindow:
            self.created.append((args, kw))
            return self.window

        def start(self) -> None:
            self.started = True

    fake = FakeWebview()
    monkeypatch.setattr(window.importlib, "import_module", lambda name: fake)
    monkeypatch.setattr(window.sys, "platform", "darwin")
    window.open_window("http://127.0.0.1:1/?token=t")
    ((args, kw),) = fake.created
    assert args == ("Sombra", "http://127.0.0.1:1/?token=t")
    assert kw["on_top"] is True
    assert fake.started
    assert len(fake.window.events.shown) == 1
    assert window.MACOS_COLLECTION_BEHAVIOR == (1 << 0) | (1 << 4) | (1 << 8)


@pytest.mark.hardware
@pytest.mark.macos
def test_real_window_stays_on_top() -> None:  # pragma: no cover - manual, needs a display
    """Manual: `uv sync --extra window && uv run pytest -m hardware tests/ui`.

    Opens the demo overlay; put Zoom/Meet in full screen and check it floats on top,
    then close the window to finish.
    """
    commands._run(_args(rounds=None, interval=6.0, think=1.5, no_clipboard=False))
