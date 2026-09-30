"""``sombra start``: a live meeting on macOS, from config to minutes (#17).

Order, as in the issue: user config (+ profile) -> meeting folder -> consent gate (a
refusal removes the folder it just made) -> the agent provider from ``[brain]`` /
``[summary]`` (a keychain key only for the API-key backends, #47) -> capture
sources, transcriber, detector, brain, overlay, summary -> :class:`Session`. Ctrl+C
(or closing the overlay window) stops the session: queues drain, minutes are written,
and the ``sombra report`` table is printed.

Platform bindings (Core Audio, ScreenCaptureKit, AppKit, pywebview) are imported only
inside :func:`build_live_sources` and the window helpers, so this module imports on
any OS; the live path itself runs only on macOS.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import signal
import sys
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sombra.config import UserConfig
from sombra.contracts import ApprovalUI, AudioSource, AutonomyLevel, Brain, ScreenSource
from sombra.orchestrator.session import local_now
from sombra.orchestrator.settings import SessionSettings
from sombra.orchestrator.wiring import (
    Assembly,
    NoBrain,
    SilentUI,
    build_agent_brain,
    build_detector,
    build_session,
    build_transcriber,
    epoch_interval_s,
    key_provider,
)
from sombra.privacy import BlockedApps, ControlServer, PauseController
from sombra.summary import TextModel

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LivePlan:
    """Everything ``sombra start`` resolved before capture begins."""

    meeting_dir: Path
    started_at: datetime
    config: UserConfig
    level: AutonomyLevel
    allowed_topics: Sequence[str] = ()
    window: str | None = None  # window-only capture: a title substring (S4)
    summary: TextModel | None = None  # rolling summaries and minutes; None: neither
    agent_key: Callable[[], str] | None = None  # `wiring.agent_key`: None on a CLI login
    blocked: BlockedApps = field(default_factory=BlockedApps)


class StopSignal:
    """Thread-safe "stop the meeting" flag for Ctrl+C, the window closing, or a test."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None
        self._early = threading.Event()

    def bind(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._event = asyncio.Event()
        if self._early.is_set():
            self._event.set()

    def request(self) -> None:
        self._early.set()
        if self._loop is not None and self._event is not None:
            self._loop.call_soon_threadsafe(self._event.set)

    async def wait(self) -> None:
        assert self._event is not None, "bind() first"  # noqa: S101 - programming error
        await self._event.wait()


def discard_meeting(meeting_dir: Path) -> None:
    """Remove a meeting folder that ``start`` just created and never recorded into."""
    shutil.rmtree(meeting_dir, ignore_errors=True)


def build_live_sources(plan: LivePlan) -> tuple[AudioSource, ScreenSource]:  # pragma: no cover
    """``MacAudioSource`` + ``MacScreenSource`` (macOS only; imported here, lazily)."""
    from sombra.audio.macos import AUTO, MacAudioSource
    from sombra.audio.reconnect import SourceStatus
    from sombra.screen.macos import CaptureMode, MacScreenSource, WindowSelector

    def on_status(status: SourceStatus) -> None:  # reconnect thread: log only (A3)
        log.warning(
            "audio %s %s %s%s",
            status.channel.value,
            status.kind,
            status.device or "",
            f" ({status.reason})" if status.reason else "",
        )

    cfg = plan.config.audio
    audio = MacAudioSource(mic=cfg.mic, system=cfg.system or AUTO, on_status=on_status)
    if plan.window:
        screen = MacScreenSource(CaptureMode.WINDOW, selector=WindowSelector(title=plan.window))
    else:
        screen = MacScreenSource()
    return audio, screen


def summary_model(plan: LivePlan) -> TextModel | None:
    return plan.summary


def build_brain(plan: LivePlan) -> Brain:
    if plan.level is AutonomyLevel.L0:
        return NoBrain()
    brain = plan.config.brain
    backend = brain.backend
    if brain.uses_api_key and plan.agent_key is None:
        provider = key_provider(backend)
        raise ValueError(f"{plan.level} needs an API key: run `sombra auth set {provider}`")
    user = plan.config.user
    return build_agent_brain(
        backend,
        plan.agent_key,
        user_name=user.name,
        aliases=user.aliases,
        allowed_topics=plan.allowed_topics,
        level=plan.level,
        model=plan.config.models.agent,
    )


async def run_live(
    plan: LivePlan,
    stop: StopSignal,
    *,
    sources: Callable[[LivePlan], tuple[AudioSource, ScreenSource]] = build_live_sources,
    ui: ApprovalUI | None = None,
    on_ui_url: Callable[[str], None] | None = None,
    control_socket: Path | None = None,
    transcriber: Any = None,
    pause: PauseController | None = None,
) -> None:
    """Run one live meeting until ``stop`` is requested; then drain, write minutes, close."""
    stop.bind()
    brain = build_brain(plan)
    user = plan.config.user
    if transcriber is None:  # pragma: no cover - needs the whisper model (hardware test)
        transcriber = build_transcriber(plan.config.models.stt, vocabulary=user.all_aliases)
    closers: list[Callable[[], Awaitable[None]]] = []
    assembly: Assembly | None = None
    try:
        if ui is None:
            if plan.level is AutonomyLevel.L0:
                ui = SilentUI()
            else:  # pragma: no cover - opens a local web server + window (manual test)
                from sombra.ui import OverlayUI

                overlay = OverlayUI(frames_dir=plan.meeting_dir / "frames")
                await overlay.start()
                closers.append(overlay.close)
                if on_ui_url is not None:
                    on_ui_url(overlay.url)
                ui = overlay
        pause = pause or PauseController()
        control = ControlServer(pause, control_socket)
        try:
            await control.start()
            closers.append(control.close)
        except OSError as e:
            log.warning("`sombra pause` will not work: control socket unavailable (%s)", e)
        audio, screen = sources(plan)
        assembly = build_session(
            plan.meeting_dir,
            audio=audio,
            transcriber=transcriber,
            screen=screen,
            detector=build_detector(user.name, user.aliases),
            brain=brain,
            ui=ui,
            settings=SessionSettings(
                autonomy=plan.level,
                screen_interval_s=plan.config.capture_interval_s,
                epoch_interval_s=epoch_interval_s(),
            ),
            started_at=plan.started_at,
            summary_model=summary_model(plan),
            on_epoch=getattr(brain, "start_epoch", None),
            pause=pause,
            blocked=plan.blocked.is_blocked,
            clock=local_now,
        )
        assembly.closers.extend(closers)
        closers = []  # the assembly closes them now
        await _run_until_stopped(assembly, stop)
    finally:
        if assembly is not None:
            await assembly.close()
        for close in reversed(closers):  # startup failed before the session existed
            with contextlib.suppress(Exception):
                await close()


async def _run_until_stopped(assembly: Assembly, stop: StopSignal) -> None:
    session = assembly.session
    task = asyncio.create_task(session.run(), name="meeting")
    stopper = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        sys.stderr.write("sombra: encerrando (gravando a ata)…\n")
        await session.stop()
        await task
    finally:
        stopper.cancel()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def run_in_terminal(plan: LivePlan, **kwargs: Any) -> None:  # pragma: no cover - live path
    """Run on the main thread; the overlay opens in a browser tab. Ctrl+C stops, a
    second Ctrl+C aborts. ``kwargs`` go to :func:`run_live` (tests pass fakes)."""
    from sombra.ui.window import open_in_browser

    stop = StopSignal()

    async def main() -> None:
        loop = asyncio.get_running_loop()

        def on_sigint() -> None:
            loop.remove_signal_handler(signal.SIGINT)  # a second Ctrl+C aborts
            stop.request()

        loop.add_signal_handler(signal.SIGINT, on_sigint)
        await run_live(plan, stop, on_ui_url=lambda url: _announce(url, open_in_browser), **kwargs)

    asyncio.run(main())


def run_with_window(plan: LivePlan, shortcut: str) -> None:  # pragma: no cover - needs macOS
    """The session in a worker thread; the always-on-top overlay and the pause shortcut
    on the main thread (Cocoa needs it there). Closing the window or Ctrl+C stops.
    """
    from sombra.privacy.hotkey import MacHotkey
    from sombra.ui.window import open_window

    stop = StopSignal()
    pause = PauseController()
    ready = threading.Event()
    box: dict[str, str] = {}
    errors: list[BaseException] = []

    def on_url(url: str) -> None:
        box["url"] = url
        ready.set()

    def worker() -> None:
        try:
            asyncio.run(run_live(plan, stop, on_ui_url=on_url, pause=pause))
        except BaseException as e:
            errors.append(e)
        finally:
            ready.set()

    # Daemon: a second Ctrl+C while stopping aborts without waiting for the minutes.
    thread = threading.Thread(target=worker, name="sombra-session", daemon=True)
    hotkey = None
    try:
        thread.start()
        ready.wait()  # a Ctrl+C during startup lands here, and the finally still stops it
        if "url" in box:
            with contextlib.suppress(Exception):
                hotkey = MacHotkey(shortcut, lambda: _toggle(pause))
                hotkey.start()
            _announce(box["url"], None)
            open_window(box["url"])
    except KeyboardInterrupt:
        pass
    finally:
        if hotkey is not None:
            hotkey.stop()
        stop.request()
        try:
            join_interruptibly(thread)
        except KeyboardInterrupt:
            sys.stderr.write("sombra: abortado; a ata não foi gravada\n")
            raise
    if errors:
        raise errors[0]


def join_interruptibly(thread: threading.Thread, poll_s: float = 0.2) -> None:
    """``thread.join()`` that a Ctrl+C can interrupt (#75).

    Python runs signal handlers only on the main thread, between bytecodes, and the OS
    may deliver SIGINT to any thread. A plain ``join()`` waits in C (on macOS, a
    condition variable that a signal on another thread never wakes), so a second
    Ctrl+C during a slow shutdown was never handled. Waking up every ``poll_s`` is.
    """
    while thread.is_alive():
        thread.join(poll_s)


def _toggle(pause: PauseController) -> None:
    paused = pause.toggle()
    sys.stderr.write("sombra: pausado\n" if paused else "sombra: gravando\n")


def _announce(url: str, opener: Callable[[str], object] | None) -> None:
    sys.stderr.write(f"sombra: overlay em {url}\n")
    if opener is not None:
        opener(url)
