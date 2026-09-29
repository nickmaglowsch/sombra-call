"""Desktop integration: system notifications (O2) and the clipboard (approve in text mode).

Both go through small command-line tools found on ``PATH`` (``osascript`` / ``pbcopy``
on macOS, ``notify-send`` / ``wl-copy`` / ``xclip`` on Linux), so there are no
platform bindings to import. When no tool is available they are no-ops: a missing
notification must never break the overlay. Arguments are passed as argv, never
through a shell, because the text comes from the meeting (untrusted).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from collections.abc import Awaitable, Callable, Sequence

log = logging.getLogger(__name__)

Notifier = Callable[[str, str], Awaitable[None]]
"""``await notify(title, body)``: show a system notification; never raises."""

Clipboard = Callable[[str], Awaitable[None]]
"""``await copy(text)``: put text on the system clipboard; never raises."""

_TIMEOUT_S = 2.0
_MAX_NOTIFICATION_CHARS = 200

# AppleScript that reads title and body from argv, so nothing is interpolated.
_OSASCRIPT = (
    "-e",
    "on run argv",
    "-e",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "-e",
    "end run",
)


async def _run(argv: Sequence[str], stdin: bytes | None = None) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(stdin), timeout=_TIMEOUT_S)
    except (OSError, TimeoutError) as e:
        log.warning("%s failed: %s", argv[0], e)


def _shorten(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= _MAX_NOTIFICATION_CHARS:
        return text
    return text[: _MAX_NOTIFICATION_CHARS - 1] + "…"


def notification_argv(
    title: str,
    body: str,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str] | None:
    """The command that shows a notification on this platform, or None if there is none."""
    title, body = _shorten(title), _shorten(body)
    if platform == "darwin":
        if exe := which("osascript"):
            return [exe, *_OSASCRIPT, title, body]
    elif exe := which("notify-send"):
        return [exe, "--app-name=Sombra", "--", title, body]
    return None


def clipboard_argv(
    platform: str = sys.platform, which: Callable[[str], str | None] = shutil.which
) -> list[str] | None:
    """The command that reads stdin into the clipboard, or None if there is none."""
    if platform == "darwin":
        exe = which("pbcopy")
        return [exe] if exe else None
    if exe := which("wl-copy"):
        return [exe]
    if exe := which("xclip"):
        return [exe, "-selection", "clipboard"]
    return None


async def system_notify(title: str, body: str) -> None:
    if argv := notification_argv(title, body):
        await _run(argv)


async def system_clipboard(text: str) -> None:
    if argv := clipboard_argv():
        await _run(argv, stdin=text.encode("utf-8"))
    else:
        log.warning("no clipboard tool found; approved text was not copied")


async def no_notify(title: str, body: str) -> None:
    """Notifier that does nothing (tests, headless runs)."""


async def no_clipboard(text: str) -> None:
    """Clipboard that does nothing (tests, headless runs)."""
