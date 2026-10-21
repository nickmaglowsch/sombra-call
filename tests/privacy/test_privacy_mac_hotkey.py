"""MacHotkey.start()/stop() with fakes of AppKit's NSEvent and AXIsProcessTrusted() (#90)."""

import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from sombra.privacy import hotkey
from sombra.privacy.hotkey import MacHotkey

CTRL, ALT, CMD = 1 << 18, 1 << 19, 1 << 20
PANE = "System Settings > Privacy & Security > Accessibility"


class FakeNSEvent:
    def __init__(self, global_monitor: object | None = "global-monitor") -> None:
        self.global_monitor = global_monitor
        self.handlers: dict[str, Callable[[Any], Any]] = {}
        self.removed: list[object] = []

    def addGlobalMonitorForEventsMatchingMask_handler_(
        self, mask: int, handler: Callable[[Any], Any]
    ) -> object | None:
        assert mask == 1 << 10
        self.handlers["global"] = handler
        return self.global_monitor

    def addLocalMonitorForEventsMatchingMask_handler_(
        self, mask: int, handler: Callable[[Any], Any]
    ) -> object:
        self.handlers["local"] = handler
        return "local-monitor"

    def removeMonitor_(self, monitor: object) -> None:
        self.removed.append(monitor)


class FakeKeyEvent:
    def __init__(self, chars: str, flags: int) -> None:
        self._chars, self._flags = chars, flags

    def charactersIgnoringModifiers(self) -> str:
        return self._chars

    def modifierFlags(self) -> int:
        return self._flags


def _install(
    monkeypatch: pytest.MonkeyPatch, ns_event: FakeNSEvent, trusted: bool | Exception
) -> None:
    monkeypatch.setattr(hotkey, "_load_appkit", lambda: SimpleNamespace(NSEvent=ns_event))

    def ax_is_process_trusted() -> bool:
        if isinstance(trusted, Exception):
            raise trusted
        return trusted

    monkeypatch.setattr(hotkey, "_ax_is_process_trusted", ax_is_process_trusted)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_trusted_installs_monitors_without_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ns_event = FakeNSEvent()
    _install(monkeypatch, ns_event, trusted=True)
    hits: list[int] = []
    hk = MacHotkey("ctrl+alt+cmd+p", lambda: hits.append(1))

    with caplog.at_level(logging.WARNING, logger="sombra.privacy.hotkey"):
        hk.start()

    assert _warnings(caplog) == []
    ns_event.handlers["global"](FakeKeyEvent("p", CTRL | ALT | CMD))
    ns_event.handlers["global"](FakeKeyEvent("p", CTRL | ALT))
    assert hits == [1]
    # The local monitor swallows the shortcut and passes every other key through.
    assert ns_event.handlers["local"](FakeKeyEvent("p", CTRL | ALT | CMD)) is None
    other = FakeKeyEvent("x", CMD)
    assert ns_event.handlers["local"](other) is other
    assert hits == [1, 1]

    hk.stop()
    assert ns_event.removed == ["global-monitor", "local-monitor"]
    hk.stop()  # idempotent
    assert ns_event.removed == ["global-monitor", "local-monitor"]


def test_untrusted_warns_naming_the_pane_and_still_starts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # macOS 27: the monitor comes back non-nil although the app isn't trusted.
    ns_event = FakeNSEvent(global_monitor="global-monitor")
    _install(monkeypatch, ns_event, trusted=False)
    hk = MacHotkey("ctrl+alt+cmd+p", lambda: None)

    with caplog.at_level(logging.WARNING, logger="sombra.privacy.hotkey"):
        hk.start()

    [warning] = _warnings(caplog)
    assert PANE in warning
    assert "⌃⌥⌘P" in warning
    assert "not trusted for Accessibility" in warning
    assert "restart" in warning
    assert "sombra pause" in warning
    assert set(ns_event.handlers) == {"global", "local"}  # the session goes on


def test_nil_monitor_warns_even_when_trusted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ns_event = FakeNSEvent(global_monitor=None)
    _install(monkeypatch, ns_event, trusted=True)
    hk = MacHotkey("shift+cmd+k", lambda: None)

    with caplog.at_level(logging.WARNING, logger="sombra.privacy.hotkey"):
        hk.start()

    [warning] = _warnings(caplog)
    assert PANE in warning
    assert "⇧⌘K" in warning
    hk.stop()
    assert ns_event.removed == ["local-monitor"]  # nil monitors aren't removed


def test_probe_failure_falls_back_to_the_nil_monitor_check(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ns_event = FakeNSEvent()
    _install(monkeypatch, ns_event, trusted=OSError("no ApplicationServices"))
    hk = MacHotkey("ctrl+alt+cmd+p", lambda: None)

    with caplog.at_level(logging.WARNING, logger="sombra.privacy.hotkey"):
        hk.start()

    assert _warnings(caplog) == []
    assert set(ns_event.handlers) == {"global", "local"}


def test_callback_error_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ns_event = FakeNSEvent()
    _install(monkeypatch, ns_event, trusted=True)

    def boom() -> None:
        raise RuntimeError("boom")

    MacHotkey("ctrl+alt+cmd+p", boom).start()
    with caplog.at_level(logging.ERROR, logger="sombra.privacy.hotkey"):
        ns_event.handlers["global"](FakeKeyEvent("p", CTRL | ALT | CMD))
    assert "pause shortcut callback failed" in caplog.text


def test_stop_before_start_does_not_load_appkit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> None:
        raise AssertionError("AppKit loaded")

    monkeypatch.setattr(hotkey, "_load_appkit", fail)
    MacHotkey("ctrl+alt+cmd+p", lambda: None).stop()
