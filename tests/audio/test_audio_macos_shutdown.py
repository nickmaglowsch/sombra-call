"""#75: a Core Audio stop that never returns must not hang ``MacAudioSource.close``."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator

import pytest
from audio_fakes import FakeSounddevice, FakeTap

import sombra.audio.macos as macos
from sombra.audio.macos import CLOSE_TIMEOUT_S, MacAudioSource

TIMEOUT_S = 0.3
MARGIN_S = 0.5


class BrokenTap(FakeTap):
    def destroy(self) -> None:
        raise OSError("AudioHardwareDestroyProcessTap failed")


def _source(sd: FakeSounddevice, *, tap: type[FakeTap] = FakeTap) -> MacAudioSource:
    return MacAudioSource(
        sounddevice=sd,
        tap_factory=lambda: tap(sd),
        macos_release="14.5",
        reconnect=False,
        close_timeout_s=TIMEOUT_S,
    )


@pytest.fixture
def stuck() -> Iterator[FakeSounddevice]:
    """A PortAudio whose ``stop()``/``abort()`` blocks until the test ends."""
    sd = FakeSounddevice()
    sd.hang_stop = threading.Event()
    yield sd
    sd.hang_stop.set()
    for t in threading.enumerate():
        if t.name == "sombra-audio-close":
            t.join(2)


@pytest.fixture
def unregistered(monkeypatch: pytest.MonkeyPatch) -> list[Callable[[], None]]:
    """What ``close`` took out of ``atexit`` (sounddevice's ``Pa_Terminate`` handler)."""
    seen: list[Callable[[], None]] = []
    monkeypatch.setattr(macos.atexit, "unregister", seen.append)
    return seen


async def _until(cond: Callable[[], bool]) -> None:
    for _ in range(1000):
        if cond():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("timed out")


def _non_daemon_threads() -> list[threading.Thread]:
    return [
        t
        for t in threading.enumerate()
        if t.is_alive() and not t.daemon and t is not threading.main_thread()
    ]


async def test_close_gives_up_on_a_stuck_stream_after_the_timeout(
    stuck: FakeSounddevice,
    unregistered: list[Callable[[], None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    stuck._exit_handler = lambda: None  # type: ignore[attr-defined]
    before = _non_daemon_threads()
    src = _source(stuck)
    src.start()
    t0 = time.monotonic()
    await src.close()
    elapsed = time.monotonic() - t0
    assert TIMEOUT_S <= elapsed < TIMEOUT_S + MARGIN_S
    assert [s.kwargs["device"] for s in stuck.hanging] == [0]  # the mic, the first channel
    assert "shutdown stuck aborting the EU stream" in caplog.text
    assert unregistered == [stuck._exit_handler]  # type: ignore[attr-defined]
    # The stuck teardown is a daemon thread: it cannot hold the interpreter's exit.
    assert _non_daemon_threads() == before
    assert [t.daemon for t in threading.enumerate() if t.name == "sombra-audio-close"] == [True]
    assert await src._queue.get() is None  # the stream() consumer is released
    await src.close()  # idempotent, and immediate


async def test_the_stream_consumer_is_released_when_its_task_is_cancelled(
    stuck: FakeSounddevice,
) -> None:
    """The live path: the session cancels the capture task; ``stream()``'s ``finally``
    runs ``close()`` inside it (the frame that hung in #75)."""
    src = _source(stuck)

    async def consume() -> None:
        async for _ in src.stream():
            pass

    task = asyncio.create_task(consume())
    await _until(lambda: len(stuck.streams) == 2)
    t0 = time.monotonic()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert time.monotonic() - t0 < TIMEOUT_S + MARGIN_S
    assert task.cancelled()


async def test_a_second_cancel_while_stuck_returns_at_once(
    stuck: FakeSounddevice, unregistered: list[Callable[[], None]]
) -> None:
    """A second Ctrl+C cancels the task waiting in ``close()``: it must not wait out
    the timeout, and the exit must still skip PortAudio's terminate."""
    stuck._exit_handler = lambda: None  # type: ignore[attr-defined]
    src = MacAudioSource(
        sounddevice=stuck,
        tap_factory=lambda: FakeTap(stuck),
        macos_release="14.5",
        reconnect=False,
        close_timeout_s=60,
    )
    src.start()
    task = asyncio.create_task(src.close())
    await _until(lambda: bool(stuck.hanging))
    t0 = time.monotonic()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert time.monotonic() - t0 < MARGIN_S
    assert unregistered == [stuck._exit_handler]  # type: ignore[attr-defined]
    assert await src._queue.get() is None


async def test_close_names_a_restart_holding_the_lock(
    caplog: pytest.LogCaptureFixture, unregistered: list[Callable[[], None]]
) -> None:
    """A reconnect stuck in Core Audio holds the source's lock; close still returns."""
    sd = FakeSounddevice()
    src = _source(sd)
    src.start()
    held, release = threading.Event(), threading.Event()

    def restart_in_core_audio() -> None:
        with src._lock:
            held.set()
            release.wait()

    holder = threading.Thread(target=restart_in_core_audio, daemon=True)
    holder.start()
    held.wait()
    try:
        await asyncio.wait_for(src.close(), TIMEOUT_S + MARGIN_S)
        assert "shutdown stuck waiting for the audio lock" in caplog.text
        assert unregistered == []  # a fake without sounddevice's exit handler
    finally:
        release.set()
        holder.join(2)


async def test_a_clean_close_aborts_closes_and_keeps_portaudio_atexit(
    unregistered: list[Callable[[], None]], caplog: pytest.LogCaptureFixture
) -> None:
    sd = FakeSounddevice()
    sd._exit_handler = lambda: None  # type: ignore[attr-defined]
    src = _source(sd)
    src.start()
    await src.close()
    assert all(s.aborted and s.closed for s in sd.streams)
    assert unregistered == []
    assert "stuck" not in caplog.text
    for t in threading.enumerate():
        if t.name == "sombra-audio-close":
            t.join(1)
            assert not t.is_alive()


async def test_a_failing_tap_destroy_is_logged_and_close_returns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sd = FakeSounddevice()
    src = _source(sd, tap=BrokenTap)
    src.start()
    await src.close()
    assert "error destroying the process tap" in caplog.text
    assert src._tap is None


async def test_close_before_start() -> None:
    src = _source(FakeSounddevice())
    await src.close()
    assert await src._queue.get() is None


def test_default_timeout() -> None:
    assert CLOSE_TIMEOUT_S == 5.0
    assert MacAudioSource()._close_timeout_s == CLOSE_TIMEOUT_S


async def test_teardown_errors_are_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    src = _source(FakeSounddevice())
    src.start()

    def boom() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(src, "_teardown", boom)
    await src.close()  # does not raise
    assert "error closing the audio source" in caplog.text
