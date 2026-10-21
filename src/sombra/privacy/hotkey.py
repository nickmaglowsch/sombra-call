"""Global pause shortcut (default ⌃⌥⌘P). The binding is macOS-only; parsing is portable.

``MacHotkey`` installs an ``NSEvent`` global + local key-down monitor, so the
shortcut works while another app (the call) is focused. macOS delivers global key
events only to apps granted Accessibility (System Settings > Privacy & Security >
Accessibility), and only while an AppKit run loop is running on the main thread
(the overlay's, in the full app).

``start()`` checks ``AXIsProcessTrusted()`` first: when the app isn't trusted, AppKit
can still return a monitor object that never receives events (seen on macOS 27,
#76/#90), so a nil monitor alone is not a reliable signal. Input Monitoring
(``CGPreflightListenEventAccess``) is not checked: it gates listen-only
``CGEventTap``/IOHID, which this module doesn't use.
"""

from __future__ import annotations

import ctypes
import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_SHORTCUT = "ctrl+alt+cmd+p"

_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "⌃": "ctrl",
    "alt": "alt",
    "opt": "alt",
    "option": "alt",
    "⌥": "alt",
    "cmd": "cmd",
    "command": "cmd",
    "super": "cmd",
    "⌘": "cmd",
    "shift": "shift",
    "⇧": "shift",
}

# NSEventModifierFlags (AppKit): stable values, avoid importing AppKit to read them.
_NS_FLAGS = {"shift": 1 << 17, "ctrl": 1 << 18, "alt": 1 << 19, "cmd": 1 << 20}
_NS_DEVICE_INDEPENDENT_MASK = 0xFFFF0000
_NS_KEY_DOWN_MASK = 1 << 10

_APPLICATION_SERVICES = (
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
)

ACCESSIBILITY_WARNING = (
    "pause shortcut {shortcut} will not work: Sombra is not trusted for Accessibility. "
    "Open System Settings > Privacy & Security > Accessibility, switch Sombra (or your "
    "terminal, when running from source) on, then restart sombra. "
    "`sombra pause` / `sombra resume` work without it."
)


@dataclass(frozen=True, slots=True)
class Shortcut:
    modifiers: frozenset[str]  # subset of {"ctrl", "alt", "cmd", "shift"}
    key: str  # one lower-case character

    @property
    def ns_modifier_flags(self) -> int:
        flags = 0
        for m in self.modifiers:
            flags |= _NS_FLAGS[m]
        return flags

    def matches(self, key: str, modifier_flags: int) -> bool:
        """True if an NSEvent's ``charactersIgnoringModifiers`` + ``modifierFlags`` match."""
        relevant = modifier_flags & _NS_DEVICE_INDEPENDENT_MASK & sum(_NS_FLAGS.values())
        return key.lower() == self.key and relevant == self.ns_modifier_flags


def parse_shortcut(text: str) -> Shortcut:
    """Parse ``"ctrl+alt+cmd+p"`` / ``"⌃⌥⌘P"``-style shortcuts. Needs at least one modifier."""
    raw = text.strip()
    if "+" in raw:
        parts = [p.strip().lower() for p in raw.split("+")]
    else:  # symbol form: every char but the last is a modifier symbol
        parts = [*raw[:-1], raw[-1:].lower()]
    *mods, key = parts
    if len(key) != 1 or not key.isprintable() or key.isspace():
        raise ValueError(f"shortcut key must be one character: {text!r}")
    modifiers = set()
    for m in mods:
        if m not in _ALIASES:
            raise ValueError(f"unknown modifier {m!r} in shortcut {text!r}")
        modifiers.add(_ALIASES[m])
    if not modifiers:
        raise ValueError(f"shortcut needs at least one modifier: {text!r}")
    return Shortcut(frozenset(modifiers), key)


def _load_appkit() -> Any:  # pragma: no cover - needs macOS
    return importlib.import_module("AppKit")


def _ax_is_process_trusted() -> bool:  # pragma: no cover - needs macOS
    lib = ctypes.cdll.LoadLibrary(_APPLICATION_SERVICES)
    lib.AXIsProcessTrusted.restype = ctypes.c_bool
    return bool(lib.AXIsProcessTrusted())


class MacHotkey:
    """Calls ``callback`` when the shortcut is pressed, whichever app is focused."""

    def __init__(self, shortcut: Shortcut | str, callback: Callable[[], None]) -> None:
        self.shortcut = parse_shortcut(shortcut) if isinstance(shortcut, str) else shortcut
        self.callback = callback
        self._monitors: list[Any] = []

    def start(self) -> None:
        """Install the monitors. Never raises for a missing grant; it logs a warning."""
        ns_event = _load_appkit().NSEvent
        trusted = self._trusted()

        def on_global(event: Any) -> None:
            self._dispatch(event)

        def on_local(event: Any) -> Any:
            return None if self._dispatch(event) else event

        self._monitors = [
            ns_event.addGlobalMonitorForEventsMatchingMask_handler_(_NS_KEY_DOWN_MASK, on_global),
            ns_event.addLocalMonitorForEventsMatchingMask_handler_(_NS_KEY_DOWN_MASK, on_local),
        ]
        if not trusted or self._monitors[0] is None:
            log.warning(ACCESSIBILITY_WARNING.format(shortcut=self._label()))

    def _trusted(self) -> bool:
        try:
            return _ax_is_process_trusted()
        except Exception:  # an unreadable answer must not stop the session
            log.debug("AXIsProcessTrusted() failed", exc_info=True)
            return True  # unknown: fall back to the nil-monitor check

    def _label(self) -> str:
        symbols = {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "cmd": "⌘"}
        mods = "".join(sym for m, sym in symbols.items() if m in self.shortcut.modifiers)
        return mods + self.shortcut.key.upper()

    def stop(self) -> None:
        if not self._monitors:
            return
        appkit = _load_appkit()
        for monitor in self._monitors:
            if monitor is not None:
                appkit.NSEvent.removeMonitor_(monitor)
        self._monitors = []

    def _dispatch(self, event: Any) -> bool:
        chars = str(event.charactersIgnoringModifiers() or "")
        if not self.shortcut.matches(chars, int(event.modifierFlags())):
            return False
        try:
            self.callback()
        except Exception:
            log.exception("pause shortcut callback failed")
        return True
