"""MacScreenSource with a fake backend: window matching, metadata, permission path."""

from __future__ import annotations

import importlib
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sombra.contracts import ScreenSource
from sombra.screen.macos import (
    CaptureMode,
    MacScreenSource,
    ScreencaptureCliBackend,
    ScreenCaptureError,
    ScreenPermissionError,
    WindowInfo,
    WindowNotFoundError,
    WindowSelector,
    capturable_windows,
    frontmost_window,
    match_window,
    parse_window_list,
)

T0 = datetime(2026, 9, 29, 14, 32, 7, tzinfo=UTC)


def raw(
    wid: int,
    app: str | None,
    title: str | None,
    *,
    layer: int = 0,
    on_screen: bool = True,
    pid: int = 100,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "kCGWindowNumber": wid,
        "kCGWindowLayer": layer,
        "kCGWindowIsOnscreen": on_screen,
        "kCGWindowOwnerPID": pid,
    }
    if app is not None:
        d["kCGWindowOwnerName"] = app
    if title is not None:
        d["kCGWindowName"] = title
    return d


DESKTOP = [
    raw(1, "Window Server", "Menubar", layer=25),
    raw(2, "Control Center", None, layer=25),
    raw(10, "zoom.us", "Zoom Meeting"),
    raw(11, "Google Chrome", "Roadmap Q4 - Google Slides"),
    raw(12, "Google Chrome", "Jira - Sprint 42"),
    raw(13, "Slack", "general", on_screen=False),
    raw(14, "Dock", "Desktop", layer=0),
]


class FakeBackend:
    def __init__(
        self,
        windows: list[dict[str, Any]] | None = None,
        *,
        permitted: bool = True,
        grant_on_request: bool = False,
    ) -> None:
        self.windows = list(DESKTOP if windows is None else windows)
        self.permitted = permitted
        self.grant_on_request = grant_on_request
        self.requests = 0
        self.captured: list[tuple[str, int]] = []
        self.gone: set[int] = set()

    def has_permission(self) -> bool:
        return self.permitted

    def request_permission(self) -> None:
        self.requests += 1
        if self.grant_on_request:
            self.permitted = True

    def list_windows(self) -> list[dict[str, Any]]:
        return self.windows

    def capture_display(self, display: int) -> bytes:
        self.captured.append(("display", display))
        return b"\x89PNG display"

    def capture_window(self, window_id: int) -> bytes:
        if window_id in self.gone:
            raise WindowNotFoundError(f"window {window_id} gone")
        self.captured.append(("window", window_id))
        return b"\x89PNG window " + str(window_id).encode()


def source(backend: FakeBackend, **kw: Any) -> MacScreenSource:
    return MacScreenSource(backend=backend, clock=lambda: T0, **kw)


# --- import & protocol ------------------------------------------------------------------


def test_imports_without_pyobjc() -> None:
    mod = importlib.import_module("sombra.screen.macos")
    assert "Quartz" not in sys.modules or sys.platform == "darwin"
    assert "ScreenCaptureKit" not in sys.modules or sys.platform == "darwin"
    assert mod.MacScreenSource is MacScreenSource


def test_satisfies_screen_source_port() -> None:
    src: ScreenSource = source(FakeBackend())
    assert callable(src.grab)
    assert callable(src.close)


# --- parsing -----------------------------------------------------------------------------


def test_parse_window_list_handles_missing_and_odd_fields() -> None:
    parsed = parse_window_list(
        [
            {"kCGWindowNumber": 5, "kCGWindowOwnerName": "Notes", "kCGWindowName": "  "},
            {"kCGWindowOwnerName": "no id"},
            {"kCGWindowNumber": "x"},
            {"kCGWindowNumber": 6, "kCGWindowOwnerPID": True, "kCGWindowLayer": None},
            {"kCGWindowNumber": 7.0, "kCGWindowOwnerPID": "42", "kCGWindowIsOnscreen": 1},
        ]
    )
    assert parsed == [
        WindowInfo(id=5, app="Notes", title=None, pid=None, layer=0, on_screen=False),
        WindowInfo(id=6, app=None, title=None, pid=None, layer=0, on_screen=False),
        WindowInfo(id=7, app=None, title=None, pid=42, layer=0, on_screen=True),
    ]


def test_capturable_windows_drops_system_and_overlay_layers() -> None:
    ids = [w.id for w in capturable_windows(parse_window_list(DESKTOP))]
    assert ids == [10, 11, 12, 13]


# --- matching ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        (WindowSelector(app="zoom.us"), 10),
        (WindowSelector(app="ZOOM.US"), 10),
        (WindowSelector(title="roadmap"), 11),
        (WindowSelector(app="Google Chrome"), 11),  # frontmost of several matches
        (WindowSelector(app="Google Chrome", title="jira"), 12),
    ],
)
def test_match_window(selector: WindowSelector, expected: int) -> None:
    assert match_window(parse_window_list(DESKTOP), selector).id == expected


def test_match_prefers_on_screen_over_front_hidden() -> None:
    windows = parse_window_list(
        [raw(1, "Chrome", "a", on_screen=False), raw(2, "Chrome", "b", on_screen=True)]
    )
    assert match_window(windows, WindowSelector(app="Chrome")).id == 2


@pytest.mark.parametrize(
    "selector",
    [
        WindowSelector(app="Chrome"),  # app must match exactly, not as a substring
        WindowSelector(title="Excel"),
        WindowSelector(app="zoom.us", title="Slides"),
        WindowSelector(title="Menubar"),  # system windows are never picked
    ],
)
def test_match_window_not_found(selector: WindowSelector) -> None:
    with pytest.raises(WindowNotFoundError, match="sombra windows"):
        match_window(parse_window_list(DESKTOP), selector)


def test_match_window_off_screen_only_raises() -> None:
    # Slack is minimised / on another Space: capturing it would give a blank frame.
    with pytest.raises(WindowNotFoundError, match="minimised or on another Space"):
        match_window(parse_window_list(DESKTOP), WindowSelector(app="Slack"))


async def test_window_mode_pauses_while_window_is_minimised() -> None:
    backend = FakeBackend()
    src = source(backend, mode="window", selector=WindowSelector(app="zoom.us"))
    await src.grab()
    backend.windows = [raw(10, "zoom.us", "Zoom Meeting", on_screen=False), *DESKTOP[3:]]
    with pytest.raises(WindowNotFoundError, match="minimised"):
        await src.grab()
    backend.windows = list(DESKTOP)
    await src.grab()
    assert backend.captured == [("window", 10), ("window", 10)]


def test_title_match_never_hits_untitled_windows() -> None:
    windows = parse_window_list([raw(1, "zoom.us", None)])
    with pytest.raises(WindowNotFoundError):
        match_window(windows, WindowSelector(title="zoom"))


def test_selector_needs_app_or_title() -> None:
    with pytest.raises(ValueError, match="app and/or a title"):
        WindowSelector()
    with pytest.raises(ValueError):
        WindowSelector(app="", title="")


def test_selector_describe() -> None:
    assert WindowSelector(app="Zoom", title="x").describe() == "app='Zoom' title~'x'"


def test_frontmost_window() -> None:
    assert frontmost_window(parse_window_list(DESKTOP)) == WindowInfo(
        id=10, app="zoom.us", title="Zoom Meeting", pid=100, layer=0, on_screen=True
    )
    assert frontmost_window(parse_window_list(DESKTOP[:2])) is None


# --- display mode ------------------------------------------------------------------------


async def test_display_mode_tags_frontmost_app_and_title() -> None:
    backend = FakeBackend()
    shot = await source(backend, display=1).grab()
    assert backend.captured == [("display", 1)]
    assert shot.ts == T0
    assert shot.image == b"\x89PNG display"
    assert (shot.app, shot.window_title) == ("zoom.us", "Zoom Meeting")


async def test_display_mode_metadata_none_when_unavailable() -> None:
    # Without titles (what macOS returns before Screen Recording is granted to the
    # listing app) or with no app window at all, metadata is None, not "" or a crash.
    backend = FakeBackend([raw(3, "Finder", None)])
    shot = await source(backend).grab()
    assert (shot.app, shot.window_title) == ("Finder", None)

    backend.windows = [raw(1, "Window Server", "Menubar", layer=25)]
    shot = await source(backend).grab()
    assert (shot.app, shot.window_title) == (None, None)


# --- window mode -------------------------------------------------------------------------


async def test_window_mode_captures_only_the_chosen_window() -> None:
    backend = FakeBackend()
    src = source(backend, mode="window", selector=WindowSelector(title="Roadmap"))
    for _ in range(3):
        shot = await src.grab()
        assert shot.image == b"\x89PNG window 11"
        assert (shot.app, shot.window_title) == ("Google Chrome", "Roadmap Q4 - Google Slides")
    assert backend.captured == [("window", 11)] * 3  # never the display


async def test_window_mode_follows_window_id_when_title_changes() -> None:
    backend = FakeBackend()
    src = source(backend, mode=CaptureMode.WINDOW, selector=WindowSelector(title="Roadmap"))
    await src.grab()
    backend.windows = [
        raw(12, "Google Chrome", "Roadmap copy"),  # now frontmost, but a different window
        raw(11, "Google Chrome", "Budget 2027 - Google Sheets"),
    ]
    shot = await src.grab()
    assert backend.captured[-1] == ("window", 11)
    assert shot.window_title == "Budget 2027 - Google Sheets"


async def test_window_mode_rematches_after_window_closes() -> None:
    backend = FakeBackend()
    src = source(backend, mode="window", selector=WindowSelector(app="zoom.us"))
    await src.grab()
    backend.windows = [raw(99, "zoom.us", "Zoom Meeting (reopened)")]
    shot = await src.grab()
    assert backend.captured[-1] == ("window", 99)
    assert shot.window_title == "Zoom Meeting (reopened)"


async def test_window_mode_missing_window_raises_and_never_captures_display() -> None:
    backend = FakeBackend()
    src = source(backend, mode="window", selector=WindowSelector(app="Keynote"))
    with pytest.raises(WindowNotFoundError):
        await src.grab()
    assert backend.captured == []


async def test_window_mode_capture_failure_resets_and_rematches() -> None:
    backend = FakeBackend()
    src = source(backend, mode="window", selector=WindowSelector(app="Google Chrome"))
    await src.grab()
    backend.gone.add(11)
    with pytest.raises(WindowNotFoundError):
        await src.grab()
    backend.windows = [w for w in backend.windows if w["kCGWindowNumber"] != 11]
    await src.grab()
    assert backend.captured == [("window", 11), ("window", 12)]
    assert all(kind == "window" for kind, _ in backend.captured)


def test_constructor_validation() -> None:
    with pytest.raises(ValueError, match="WindowSelector"):
        MacScreenSource("window", backend=FakeBackend())
    with pytest.raises(ValueError, match="display"):
        MacScreenSource(display=-1, backend=FakeBackend())
    with pytest.raises(ValueError):
        MacScreenSource("region", backend=FakeBackend())


# --- permission --------------------------------------------------------------------------


async def test_permission_missing_raises_clear_error_and_requests_once() -> None:
    backend = FakeBackend(permitted=False)
    src = source(backend)
    for _ in range(3):
        with pytest.raises(ScreenPermissionError) as exc:
            await src.grab()
        assert "Screen Recording" in str(exc.value)
        assert "System Settings" in str(exc.value)
    assert backend.requests == 1
    assert backend.captured == []


async def test_permission_granted_on_request_proceeds() -> None:
    backend = FakeBackend(permitted=False, grant_on_request=True)
    shot = await source(backend).grab()
    assert backend.requests == 1
    assert shot.image == b"\x89PNG display"


def test_list_windows_needs_permission() -> None:
    backend = FakeBackend(permitted=False)
    with pytest.raises(ScreenPermissionError):
        source(backend).list_windows()
    backend.permitted = True
    assert [w.id for w in source(backend).list_windows()] == [10, 11, 12, 13]


def test_permission_error_is_a_capture_error() -> None:
    assert issubclass(ScreenPermissionError, ScreenCaptureError)


async def test_closed_source_refuses_to_grab() -> None:
    src = source(FakeBackend())
    await src.close()
    with pytest.raises(ScreenCaptureError, match="closed"):
        await src.grab()


# --- screencapture fallback --------------------------------------------------------------


class FakeRunner:
    def __init__(self, returncode: int = 0, write: bytes | None = b"\x89PNG cli") -> None:
        self.returncode = returncode
        self.write = write
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(argv)
        if self.write is not None:
            Path(argv[-1]).write_bytes(self.write)
        return subprocess.CompletedProcess(argv, self.returncode, b"", b"could not create image")


def cli_backend(runner: FakeRunner) -> ScreencaptureCliBackend:
    return ScreencaptureCliBackend(
        run=runner,
        has_permission=lambda: True,
        request_permission=lambda: None,
        list_windows=lambda: list(DESKTOP),
    )


def test_cli_backend_display_and_window_args() -> None:
    runner = FakeRunner()
    b = cli_backend(runner)
    assert b.capture_display(0) == b"\x89PNG cli"
    assert b.capture_window(11) == b"\x89PNG cli"
    display_argv, window_argv = runner.calls
    assert display_argv[:5] == ["/usr/sbin/screencapture", "-x", "-t", "png", "-D"]
    assert display_argv[5] == "1"  # screencapture counts displays from 1
    assert window_argv[4:7] == ["-o", "-l", "11"]
    assert not Path(display_argv[-1]).exists()  # temp file cleaned up


@pytest.mark.parametrize(("code", "write"), [(1, None), (0, None), (0, b"")])
def test_cli_backend_failure(code: int, write: bytes | None) -> None:
    with pytest.raises(ScreenCaptureError, match="screencapture failed"):
        cli_backend(FakeRunner(code, write)).capture_display(0)


async def test_source_works_with_cli_backend() -> None:
    runner = FakeRunner()
    src = MacScreenSource(
        "window", selector=WindowSelector(title="Jira"), backend=cli_backend(runner)
    )
    shot = await src.grab()
    assert shot.image == b"\x89PNG cli"
    assert (shot.app, shot.window_title) == ("Google Chrome", "Jira - Sprint 42")
    assert shot.ts.tzinfo is not None
