"""The overlay window: the served page inside an always-on-top pywebview window.

pywebview is an optional dependency (``uv sync --extra window``) and is imported
lazily, so ``sombra.ui`` imports on every OS and in CI. Without it, callers fall back
to a browser tab (``open_in_browser``).

``open_window`` must run on the main thread (a Cocoa requirement) and blocks until
the window is closed; run the asyncio side (``OverlayUI``) in another thread.
"""

from __future__ import annotations

import importlib
import logging
import sys
import webbrowser
from typing import Any

log = logging.getLogger(__name__)

# NSWindowCollectionBehavior: show on every Space, including over another app's
# full-screen Space (Zoom/Meet), and do not move with Mission Control.
_NS_CAN_JOIN_ALL_SPACES = 1 << 0
_NS_STATIONARY = 1 << 4
_NS_FULL_SCREEN_AUXILIARY = 1 << 8
MACOS_COLLECTION_BEHAVIOR = _NS_CAN_JOIN_ALL_SPACES | _NS_STATIONARY | _NS_FULL_SCREEN_AUXILIARY


def window_available() -> bool:
    try:
        importlib.import_module("webview")
    except ImportError:
        return False
    return True


def open_window(
    url: str,
    *,
    title: str = "Sombra",
    width: int = 420,
    height: int = 560,
    on_top: bool = True,
) -> None:
    """Show ``url`` in an always-on-top window and block until it is closed.

    Raises ``ImportError`` if pywebview is not installed.
    """
    webview: Any = importlib.import_module("webview")
    window = webview.create_window(
        title, url, width=width, height=height, on_top=on_top, resizable=True
    )
    if sys.platform == "darwin" and on_top:
        window.events.shown += lambda: _float_over_full_screen(window)
    webview.start()


def _float_over_full_screen(window: Any) -> None:  # pragma: no cover - needs macOS
    """Let the NSWindow appear over full-screen apps (pywebview's on_top alone does not)."""
    try:
        app_helper: Any = importlib.import_module("PyObjCTools.AppHelper")
        native = window.native  # the NSWindow on the Cocoa backend
        app_helper.callAfter(native.setCollectionBehavior_, MACOS_COLLECTION_BEHAVIOR)
    except Exception:
        log.exception("could not make the overlay float over full-screen windows")


def open_in_browser(url: str) -> bool:
    """Fallback when pywebview is missing: a normal browser tab (not always on top)."""
    return webbrowser.open(url)
