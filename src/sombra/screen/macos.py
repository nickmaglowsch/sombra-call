"""macOS screen capture: periodic screenshots with window metadata (PRD S1, S4).

Owns ``MacScreenSource``, the macOS implementation of ``contracts.ScreenSource``:

- **display mode** captures one monitor and tags the shot with the frontmost app and
  window title (the metadata S3 and S6 rely on);
- **window mode** (S4) captures one window, picked by app and/or title, and nothing
  else, even when other windows cover it. If the window is gone, ``grab()`` raises
  ``WindowNotFoundError`` rather than falling back to the whole display.

Capture goes through ScreenCaptureKit (``SCScreenshotManager``, macOS 14+) via pyobjc.
``ScreencaptureCliBackend`` is a fallback that shells out to ``screencapture -x``; it
costs a process spawn plus a PNG round-trip through a temp file per shot (an unmeasured
estimate of 100-250 ms instead of tens of ms), so use it only where ScreenCaptureKit is
missing.

Window metadata comes from ``CGWindowListCopyWindowInfo``. Without the Screen
Recording permission macOS omits window titles, so ``window_title`` is ``None``
then; ``app`` is ``None`` when no normal window is frontmost.

pyobjc is imported lazily, inside the backends, so ``import sombra.screen.macos``
works on every OS. The matching and metadata logic is pure and tested with a fake
backend; see ``docs/macos-screen.md`` for permissions and picking window mode.
"""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from sombra.contracts import Screenshot

PERMISSION_HELP = (
    "Sombra needs the Screen Recording permission to capture the screen. Open System "
    "Settings > Privacy & Security > Screen Recording, enable the app you run sombra "
    "from (Terminal, iTerm, your IDE, ...), then restart that app. "
    "See docs/macos-screen.md."
)

NORMAL_WINDOW_LAYER = 0  # kCGWindowLayer of ordinary app windows; menus, docks etc. are > 0

# Apps whose layer-0 windows are part of the desktop, not something the user works in.
_SYSTEM_OWNERS = frozenset({"Window Server", "Dock", "WindowManager", "Control Center"})


class ScreenCaptureError(RuntimeError):
    """Base class for capture failures the caller can report to the user."""


class ScreenPermissionError(ScreenCaptureError):
    """Screen Recording permission is not granted."""

    def __init__(self, message: str = PERMISSION_HELP) -> None:
        super().__init__(message)


class WindowNotFoundError(ScreenCaptureError):
    """Window mode: no capturable window matches the selector (closed, minimised, ...)."""


class CaptureMode(StrEnum):
    DISPLAY = "display"
    WINDOW = "window"


@dataclass(frozen=True, slots=True)
class WindowInfo:
    """One entry of the window server's list, front-to-back order."""

    id: int
    app: str | None
    title: str | None
    pid: int | None
    layer: int
    on_screen: bool


@dataclass(frozen=True, slots=True)
class WindowSelector:
    """Picks the window for window mode. Matching is case-insensitive.

    ``app`` must equal the owning app's name; ``title`` is a substring of the window
    title. At least one is required. When several windows match, the frontmost
    on-screen one wins.
    """

    app: str | None = None
    title: str | None = None

    def __post_init__(self) -> None:
        if not (self.app or self.title):
            raise ValueError("window mode needs an app and/or a title to match")

    def matches(self, w: WindowInfo) -> bool:
        if self.app and (w.app is None or w.app.casefold() != self.app.casefold()):
            return False
        return not self.title or (
            w.title is not None and self.title.casefold() in w.title.casefold()
        )

    def describe(self) -> str:
        parts = []
        if self.app:
            parts.append(f"app={self.app!r}")
        if self.title:
            parts.append(f"title~{self.title!r}")
        return " ".join(parts)


# --- pure logic ------------------------------------------------------------------------


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_window_list(raw: Sequence[Mapping[str, Any]]) -> list[WindowInfo]:
    """Convert ``CGWindowListCopyWindowInfo`` dicts, keeping order; skip entries without an id."""
    out = []
    for entry in raw:
        wid = _opt_int(entry.get("kCGWindowNumber"))
        if wid is None:
            continue
        out.append(
            WindowInfo(
                id=wid,
                app=_opt_str(entry.get("kCGWindowOwnerName")),
                title=_opt_str(entry.get("kCGWindowName")),
                pid=_opt_int(entry.get("kCGWindowOwnerPID")),
                layer=_opt_int(entry.get("kCGWindowLayer")) or 0,
                on_screen=bool(entry.get("kCGWindowIsOnscreen", False)),
            )
        )
    return out


def capturable_windows(windows: Sequence[WindowInfo]) -> list[WindowInfo]:
    """Ordinary app windows a user could pick for window mode, front-to-back."""
    return [
        w
        for w in windows
        if w.layer == NORMAL_WINDOW_LAYER and w.app is not None and w.app not in _SYSTEM_OWNERS
    ]


def frontmost_window(windows: Sequence[WindowInfo]) -> WindowInfo | None:
    """The frontmost on-screen app window, or None (e.g. only the desktop is showing)."""
    return next((w for w in capturable_windows(windows) if w.on_screen), None)


def match_window(windows: Sequence[WindowInfo], selector: WindowSelector) -> WindowInfo:
    """The frontmost on-screen window the selector picks.

    Covered windows count as on screen. Minimised windows and windows on another Space
    do not: they cannot be captured, so they raise instead of yielding a blank frame.
    """
    candidates = [w for w in capturable_windows(windows) if selector.matches(w)]
    if not candidates:
        raise WindowNotFoundError(
            f"no window matches {selector.describe()}; run `sombra windows` to list them"
        )
    visible = next((w for w in candidates if w.on_screen), None)
    if visible is None:
        raise WindowNotFoundError(
            f"the window matching {selector.describe()} is minimised or on another Space"
        )
    return visible


# --- platform backends -----------------------------------------------------------------


class CaptureBackend(Protocol):
    """The platform calls ``MacScreenSource`` needs; faked in tests. All calls block."""

    def has_permission(self) -> bool: ...

    def request_permission(self) -> None:
        """Show the system prompt (macOS shows it at most once per app)."""
        ...

    def list_windows(self) -> list[dict[str, Any]]:
        """Raw ``CGWindowListCopyWindowInfo`` dicts, front-to-back."""
        ...

    def capture_display(self, display: int) -> bytes:
        """PNG of display number ``display`` (0 = main), full resolution."""
        ...

    def capture_window(self, window_id: int) -> bytes:
        """PNG of that window alone, even when occluded."""
        ...


def _import(name: str) -> Any:  # pragma: no cover - macOS only
    """Import a pyobjc framework lazily; typed Any since pyobjc ships no stubs."""
    return importlib.import_module(name)


def _quartz_window_list() -> list[dict[str, Any]]:  # pragma: no cover - macOS only
    quartz = _import("Quartz")
    opts = quartz.kCGWindowListOptionAll | quartz.kCGWindowListExcludeDesktopElements
    raw = quartz.CGWindowListCopyWindowInfo(opts, quartz.kCGNullWindowID) or []
    return [dict(entry) for entry in raw]


def _quartz_has_permission() -> bool:  # pragma: no cover - macOS only
    return bool(_import("Quartz").CGPreflightScreenCaptureAccess())


def _quartz_request_permission() -> None:  # pragma: no cover - macOS only
    _import("Quartz").CGRequestScreenCaptureAccess()


class ScreenCaptureKitBackend:  # pragma: no cover - needs macOS 14+ and a real screen
    """``SCScreenshotManager`` one-shot captures (macOS 14+)."""

    def __init__(self, timeout_s: float = 5.0) -> None:
        self._sck = _import("ScreenCaptureKit")
        if not hasattr(self._sck, "SCScreenshotManager"):
            raise ImportError("SCScreenshotManager needs macOS 14+")
        self._quartz = _import("Quartz")
        self._timeout_s = timeout_s

    def has_permission(self) -> bool:
        return _quartz_has_permission()

    def request_permission(self) -> None:
        _quartz_request_permission()

    def list_windows(self) -> list[dict[str, Any]]:
        return _quartz_window_list()

    def _wait(self, start: Callable[[Callable[..., None]], None], what: str) -> Any:
        """Run a completion-handler API synchronously (handlers fire on an SCK queue)."""
        done = threading.Event()
        box: dict[str, Any] = {}

        def handler(result: Any, error: Any) -> None:
            box["result"], box["error"] = result, error
            done.set()

        start(handler)
        if not done.wait(self._timeout_s):
            raise ScreenCaptureError(f"ScreenCaptureKit timed out on {what}")
        if box["error"] is not None:
            # -3801 = SCStreamErrorUserDeclined: no Screen Recording permission.
            if getattr(box["error"], "code", lambda: None)() == -3801:
                raise ScreenPermissionError()
            raise ScreenCaptureError(f"ScreenCaptureKit {what} failed: {box['error']}")
        return box["result"]

    def _content(self) -> Any:
        # onScreenWindowsOnly=False so a covered or off-space target window is still found.
        content = self._sck.SCShareableContent
        return self._wait(
            lambda h: (
                content.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                    True, False, h
                )
            ),
            "listing shareable content",
        )

    def _shoot(self, content_filter: Any, width: int, height: int) -> bytes:
        config = self._sck.SCStreamConfiguration.alloc().init()
        config.setWidth_(width)
        config.setHeight_(height)
        config.setShowsCursor_(False)
        manager = self._sck.SCScreenshotManager
        image = self._wait(
            lambda h: manager.captureImageWithFilter_configuration_completionHandler_(
                content_filter, config, h
            ),
            "screenshot",
        )
        return self._png(image)

    def _png(self, image: Any) -> bytes:
        q = self._quartz
        data = q.CFDataCreateMutable(None, 0)
        dest = q.CGImageDestinationCreateWithData(data, "public.png", 1, None)
        q.CGImageDestinationAddImage(dest, image, None)
        if not q.CGImageDestinationFinalize(dest):
            raise ScreenCaptureError("PNG encoding failed")
        return bytes(data)

    def capture_display(self, display: int) -> bytes:
        err, ids, count = self._quartz.CGGetActiveDisplayList(16, None, None)
        if err or display >= count:
            raise ScreenCaptureError(f"display {display} not found ({count} active)")
        wanted = ids[display]
        scd = next((d for d in self._content().displays() if d.displayID() == wanted), None)
        if scd is None:
            raise ScreenCaptureError(f"display {display} is not capturable")
        content_filter = self._sck.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
            scd, []
        )
        # scd.width() is in points; capture at native pixels (2x on Retina).
        mode = self._quartz.CGDisplayCopyDisplayMode(wanted)
        pixel_width = self._quartz.CGDisplayModeGetPixelWidth(mode) if mode else scd.width()
        scale = pixel_width / max(1, scd.width())
        return self._shoot(content_filter, int(scd.width() * scale), int(scd.height() * scale))

    def capture_window(self, window_id: int) -> bytes:
        scw = next((w for w in self._content().windows() if w.windowID() == window_id), None)
        if scw is None:
            raise WindowNotFoundError(f"window {window_id} is not capturable (closed?)")
        content_filter = self._sck.SCContentFilter.alloc().initWithDesktopIndependentWindow_(scw)
        scale = float(content_filter.pointPixelScale())
        frame = scw.frame()
        return self._shoot(
            content_filter, int(frame.size.width * scale), int(frame.size.height * scale)
        )


Runner = Callable[[list[str]], "subprocess.CompletedProcess[bytes]"]


def _run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:  # pragma: no cover - macOS only
    return subprocess.run(argv, capture_output=True, check=False, timeout=10)  # noqa: S603


class ScreencaptureCliBackend:
    """Fallback via ``/usr/sbin/screencapture -x``; one process + temp file per shot."""

    BINARY = "/usr/sbin/screencapture"

    def __init__(
        self,
        run: Runner = _run,
        has_permission: Callable[[], bool] = lambda: _quartz_has_permission(),
        request_permission: Callable[[], None] = lambda: _quartz_request_permission(),
        list_windows: Callable[[], list[dict[str, Any]]] = lambda: _quartz_window_list(),
    ) -> None:
        self._run = run
        self.has_permission = has_permission
        self.request_permission = request_permission
        self.list_windows = list_windows

    def _shoot(self, args: list[str]) -> bytes:
        with tempfile.TemporaryDirectory(prefix="sombra-shot-") as tmp:
            out = Path(tmp) / "shot.png"
            # -x: no sound; -t png; -o: no window shadow (window mode only, harmless otherwise)
            proc = self._run([self.BINARY, "-x", "-t", "png", *args, str(out)])
            if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                stderr = proc.stderr.decode(errors="replace").strip()
                raise ScreenCaptureError(
                    f"screencapture failed (exit {proc.returncode}): {stderr or 'no image'}"
                )
            return out.read_bytes()

    def capture_display(self, display: int) -> bytes:
        return self._shoot(["-D", str(display + 1)])  # screencapture numbers displays from 1

    def capture_window(self, window_id: int) -> bytes:
        return self._shoot(["-o", "-l", str(window_id)])


def default_backend() -> CaptureBackend:  # pragma: no cover - macOS only
    """ScreenCaptureKit when pyobjc has it, else the ``screencapture`` fallback."""
    try:
        return ScreenCaptureKitBackend()
    except ImportError:
        return ScreencaptureCliBackend()


# --- the source ------------------------------------------------------------------------


class MacScreenSource:
    """``ScreenSource`` for macOS. Call ``grab()`` every 5-10 s (the caller owns the timer).

    ``mode=DISPLAY`` captures monitor ``display`` (0 = main). ``mode=WINDOW`` captures
    only the window ``selector`` picks; it is resolved once, followed by id while it
    lives (its title may change, e.g. a browser tab), and re-matched if it is closed.
    While it is minimised or on another Space, ``grab()`` raises ``WindowNotFoundError``.

    On the first grab without Screen Recording permission the system prompt is shown
    once; every grab without it raises ``ScreenPermissionError``.
    """

    def __init__(
        self,
        mode: CaptureMode | str = CaptureMode.DISPLAY,
        *,
        display: int = 0,
        selector: WindowSelector | None = None,
        backend: CaptureBackend | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.mode = CaptureMode(mode)
        if self.mode is CaptureMode.WINDOW and selector is None:
            raise ValueError("window mode needs a WindowSelector")
        if display < 0:
            raise ValueError("display must be >= 0")
        self.display = display
        self.selector = selector
        self._backend = backend
        self._clock = clock
        self._window_id: int | None = None
        self._permission_requested = False
        self._closed = False

    @property
    def backend(self) -> CaptureBackend:
        if self._backend is None:
            self._backend = default_backend()  # pragma: no cover - macOS only
        return self._backend

    def _check_permission(self) -> None:
        backend = self.backend
        if backend.has_permission():
            return
        if not self._permission_requested:
            self._permission_requested = True
            backend.request_permission()
            if backend.has_permission():
                return
        raise ScreenPermissionError()

    def _resolve_window(self, windows: list[WindowInfo]) -> WindowInfo:
        assert self.selector is not None  # noqa: S101  (guarded in __init__)
        if self._window_id is not None:
            same = next((w for w in windows if w.id == self._window_id), None)
            if same is not None:
                if not same.on_screen:  # keep following it once it is restored
                    raise WindowNotFoundError(
                        f"window {same.id} is minimised or on another Space; not capturing"
                    )
                return same
        chosen = match_window(windows, self.selector)
        self._window_id = chosen.id
        return chosen

    def grab_sync(self) -> Screenshot:
        """Blocking capture; ``grab()`` runs this in a worker thread."""
        if self._closed:
            raise ScreenCaptureError("screen source is closed")
        self._check_permission()
        ts = self._clock()
        windows = parse_window_list(self.backend.list_windows())
        if self.mode is CaptureMode.WINDOW:
            target = self._resolve_window(windows)
            try:
                image = self.backend.capture_window(target.id)
            except WindowNotFoundError:
                self._window_id = None  # re-match next time; never fall back to the display
                raise
            return Screenshot(ts=ts, image=image, app=target.app, window_title=target.title)
        front = frontmost_window(windows)
        image = self.backend.capture_display(self.display)
        return Screenshot(
            ts=ts,
            image=image,
            app=front.app if front else None,
            window_title=front.title if front else None,
        )

    async def grab(self) -> Screenshot:
        return await asyncio.to_thread(self.grab_sync)

    async def close(self) -> None:
        self._closed = True

    def list_windows(self) -> list[WindowInfo]:
        """Capturable windows, front-to-back (what ``sombra windows`` prints)."""
        self._check_permission()
        return capturable_windows(parse_window_list(self.backend.list_windows()))
