"""Seams the session needs from modules that have no port in ``sombra.contracts``.

The summarizer, the prompt-prefix epoch hook, the pause shortcut, the blocked-app
list and frame-path resolution belong to ``summary``, ``brain`` and ``privacy``.
The session takes them as small callables/protocols so it never imports those
packages; #17 passes the real ones in.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sombra.contracts import Usage


@dataclass(frozen=True, slots=True)
class EpochSummary:
    """What a summarizer reports after writing epoch ``n`` to ``summary.md`` (C4)."""

    model: str
    usage: Usage = field(default_factory=Usage)


Summarizer = Callable[[int], Awaitable[EpochSummary]]
"""Write the rolling summary for the given epoch number (1, 2, ...)."""

EpochHook = Callable[[int], None]
"""Tell the prompt-prefix builder that epoch ``n`` has started (new stable prefix)."""

MinutesWriter = Callable[[], Awaitable[None]]
"""Write the end-of-meeting minutes and action items (M3); called once by ``stop()``."""

BlockedApp = Callable[[str | None, str | None], bool]
"""``(app, window_title) -> True`` when the screenshot must not be kept."""

FrameResolver = Callable[[str], Path | None]
"""Frame id (``f0123``) -> absolute image path, or ``None`` if it cannot be found."""


class PauseState(Protocol):
    """Read side of the pause shortcut. The session polls it; it never changes it."""

    def is_paused(self) -> bool: ...


class PauseSwitch:
    """Minimal in-memory :class:`PauseState` that the pause shortcut (or a test) flips."""

    def __init__(self) -> None:
        self._paused = False

    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False


def never_blocked(app: str | None, window_title: str | None) -> bool:
    """Default :data:`BlockedApp`: keep every screenshot."""
    return False
