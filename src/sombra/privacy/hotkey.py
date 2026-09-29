"""Global pause shortcut (default ⌃⌥⌘P). The binding is macOS-only; parsing is portable.

``MacHotkey`` installs an ``NSEvent`` global + local key-down monitor, so the
shortcut works while another app (the call) is focused. macOS delivers global key
events only to apps granted Accessibility (System Settings > Privacy & Security >
Accessibility), and only while an AppKit run loop is running on the main thread
(the overlay's, in the full app).
"""

from __future__ import annotations

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


class MacHotkey:  # pragma: no cover - needs macOS, AppKit and the Accessibility permission
    """Calls ``callback`` when the shortcut is pressed, whichever app is focused."""

    def __init__(self, shortcut: Shortcut | str, callback: Callable[[], None]) -> None:
        self.shortcut = parse_shortcut(shortcut) if isinstance(shortcut, str) else shortcut
        self.callback = callback
        self._monitors: list[Any] = []

    def start(self) -> None:
        appkit: Any = importlib.import_module("AppKit")
        ns_event = appkit.NSEvent

        def on_global(event: Any) -> None:
            self._dispatch(event)

        def on_local(event: Any) -> Any:
            return None if self._dispatch(event) else event

        self._monitors = [
            ns_event.addGlobalMonitorForEventsMatchingMask_handler_(_NS_KEY_DOWN_MASK, on_global),
            ns_event.addLocalMonitorForEventsMatchingMask_handler_(_NS_KEY_DOWN_MASK, on_local),
        ]
        if self._monitors[0] is None:
            log.warning("global shortcut not installed: grant Sombra Accessibility access")

    def stop(self) -> None:
        if not self._monitors:
            return
        appkit: Any = importlib.import_module("AppKit")
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
