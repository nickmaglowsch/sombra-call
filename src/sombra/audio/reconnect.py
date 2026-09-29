"""Platform-neutral reconnect supervisor for capture channels (PRD A3).

Headsets get unplugged and outputs change mid-call. A :class:`ReconnectSupervisor`
notices that a channel stopped (reported by the backend, or no audio for
``stall_timeout_s``), restarts only that channel through a backend callable with
exponential backoff, and confirms the restart by the first new frame. Every
transition is published as a :class:`SourceStatus` so the orchestrator can log
"source lost / restored" and show the gap in the UI.

One worker thread per source does all the checking and restarting, however often a
device flaps, so reconnects never pile up threads. Audio callbacks only call
:meth:`ReconnectSupervisor.frame`, which is a single dict store.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sombra.audio.clock import NS, MonotonicClock
from sombra.contracts import Channel

log = logging.getLogger(__name__)

StatusKind = Literal["lost", "retrying", "restored"]

#: ``restart(channel, attempt)`` reopens ``channel`` (attempt counts from 1) and returns
#: the id of every channel it (re)opened. It may reopen the other channel too, when the
#: backend cannot open a device without re-enumerating; that channel is then confirmed
#: like the one that was lost. Raise to fail the attempt.
RestartFn = Callable[[Channel, int], Mapping[Channel, str]]
#: ``check()`` runs on the supervisor thread after :meth:`ReconnectSupervisor.poke` and
#: returns the channels to restart, with a reason.
CheckFn = Callable[[], Sequence[tuple[Channel, str]]]
StatusHook = Callable[["SourceStatus"], None]


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """One capture-source transition, for the orchestrator's log and the UI.

    ``lost``: the channel stopped delivering audio (``reason`` says why). ``retrying``:
    a restart attempt failed, next one after the backoff. ``restored``: audio flows
    again from ``device``; ``gap_s`` is loss -> first new frame.
    """

    channel: Channel
    kind: StatusKind
    at: datetime
    reason: str = ""
    device: str | None = None
    attempt: int = 0
    gap_s: float | None = None


@dataclass(frozen=True, slots=True)
class Backoff:
    """Delay before retry ``attempt + 1`` after ``attempt`` failed: 0.1, 0.2, 0.4 ... 2 s."""

    initial_s: float = 0.1
    factor: float = 2.0
    max_s: float = 2.0

    def delay_s(self, attempt: int) -> float:
        return min(self.max_s, self.initial_s * self.factor ** max(0, attempt - 1))


@dataclass
class _Chan:
    state: Literal["running", "lost", "confirming"] = "running"
    reason: str = ""
    lost_ns: int = 0
    attempt: int = 0
    next_ns: int = 0
    restart_ns: int = 0
    device: str | None = None


class ReconnectSupervisor:
    """Watches capture channels and restarts the ones that stop.

    Args:
        restart: see :data:`RestartFn`.
        channels: the channels to watch.
        check: see :data:`CheckFn` (device-change notifications land here).
        on_status: called on the supervisor thread for every transition; keep it quick
            and thread-safe (e.g. hand off with ``loop.call_soon_threadsafe``).
        stall_timeout_s: a channel that delivered audio and then nothing for this long
            is lost. Armed by a channel's first frame, so a device that never starts is
            the caller's startup error, not a reconnect.
        confirm_timeout_s: a restart with no frame after this long failed.
        tick_s: watchdog period of the worker thread.
    """

    def __init__(
        self,
        restart: RestartFn,
        channels: Sequence[Channel] = (Channel.ME, Channel.OTHERS),
        *,
        check: CheckFn | None = None,
        on_status: StatusHook | None = None,
        clock: MonotonicClock | None = None,
        backoff: Backoff | None = None,
        stall_timeout_s: float = 0.5,
        confirm_timeout_s: float = 1.0,
        tick_s: float = 0.1,
    ) -> None:
        self._restart = restart
        self._check = check
        self._on_status = on_status
        self._clock = clock or MonotonicClock()
        self._backoff = backoff or Backoff()
        self._stall_ns = int(stall_timeout_s * NS)
        self._confirm_ns = int(confirm_timeout_s * NS)
        self._tick_s = tick_s
        self._chans = {ch: _Chan() for ch in channels}
        # Written by audio threads, read by the worker: plain dict stores are atomic.
        self._last_frame: dict[Channel, int] = {}
        self._cond = threading.Condition()
        self._reports: list[tuple[Channel, str]] = []
        self._poked = False
        self._stopping = False
        self._thread: threading.Thread | None = None

    # --- any thread ---------------------------------------------------------------------

    def frame(self, channel: Channel) -> None:
        """Audio-thread hook: ``channel`` just delivered audio. Never blocks."""
        self._last_frame[channel] = self._clock.now_ns()

    def report_lost(self, channel: Channel, reason: str) -> None:
        """Ask for ``channel`` to be restarted (e.g. its device disappeared)."""
        with self._cond:
            self._reports.append((channel, reason))
            self._cond.notify()

    def poke(self) -> None:
        """Devices changed: run ``check`` on the supervisor thread soon."""
        with self._cond:
            self._poked = True
            self._cond.notify()

    def state(self, channel: Channel) -> str:
        return self._chans[channel].state

    # --- lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="sombra-audio-reconnect", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the worker and wait for it; a restart in progress finishes first."""
        with self._cond:
            self._stopping = True
            self._cond.notify()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._stopping:
                    return
            try:
                self.step()
            except Exception:  # the watchdog must outlive any backend bug
                log.exception("audio reconnect step failed")
            with self._cond:
                if not (self._stopping or self._reports or self._poked):
                    self._cond.wait(self._tick_s)

    # --- worker -------------------------------------------------------------------------

    def step(self) -> None:
        """One supervisor pass. Runs on the worker thread; tests call it directly."""
        with self._cond:
            reports, self._reports = self._reports, []
            poked, self._poked = self._poked, False
        if poked and self._check is not None:
            try:
                reports += self._check()
            except Exception:
                log.exception("audio device check failed")
        now = self._clock.now_ns()
        for channel, reason in reports:
            self._mark_lost(channel, reason, now)
        for channel, c in self._chans.items():
            last = self._last_frame.get(channel)
            if c.state == "running":
                if last is not None and now - last > self._stall_ns:
                    self._mark_lost(channel, f"no audio for {(now - last) / NS:.1f} s", now)
            elif c.state == "confirming":
                if last is not None and last >= c.restart_ns:
                    c.state = "running"
                    self._emit(channel, "restored", c, at_ns=last, gap_s=(last - c.lost_ns) / NS)
                elif now - c.restart_ns > self._confirm_ns:
                    self._fail(channel, c, "no audio after restart", now)
        for channel, c in self._chans.items():
            if c.state == "lost" and self._clock.now_ns() >= c.next_ns:
                self._attempt(channel, c)

    def _mark_lost(self, channel: Channel, reason: str, now: int) -> None:
        c = self._chans.get(channel)
        if c is None:
            return
        if c.state == "running":
            c.lost_ns, c.attempt, c.next_ns = now, 0, now
            c.state, c.reason = "lost", reason
            self._emit(channel, "lost", c, at_ns=now)
        elif c.state == "confirming":  # lost again before it came back: retry now
            c.state, c.reason, c.next_ns = "lost", reason, now
        # already "lost": a retry is scheduled; repeated notifications coalesce.

    def _attempt(self, channel: Channel, c: _Chan) -> None:
        c.attempt += 1
        t0 = self._clock.now_ns()
        self._last_frame.pop(channel, None)  # only a frame from the new stream confirms
        try:
            opened = dict(self._restart(channel, c.attempt))
        except Exception as e:
            log.warning("audio: restarting %s failed (attempt %d): %s", channel.value, c.attempt, e)
            self._fail(channel, c, str(e) or type(e).__name__, self._clock.now_ns())
            return
        for ch, device in opened.items():
            other = self._chans.get(ch)
            if other is None:
                continue
            if ch is not channel:
                self._last_frame.pop(ch, None)
            if ch is not channel and other.state == "running":
                other.lost_ns, other.attempt = t0, 1
                other.reason = f"reopened with {channel.value}"
                self._emit(ch, "lost", other, at_ns=t0)
            other.state, other.restart_ns, other.device = "confirming", t0, device

    def _fail(self, channel: Channel, c: _Chan, reason: str, now: int) -> None:
        c.state, c.reason = "lost", reason
        c.next_ns = now + int(self._backoff.delay_s(c.attempt) * NS)
        self._emit(channel, "retrying", c, at_ns=now)

    def _emit(
        self,
        channel: Channel,
        kind: StatusKind,
        c: _Chan,
        *,
        at_ns: int,
        gap_s: float | None = None,
    ) -> None:
        status = SourceStatus(
            channel=channel,
            kind=kind,
            at=self._clock.to_wall(at_ns),
            reason="" if kind == "restored" else c.reason,
            device=c.device,
            attempt=c.attempt,
            gap_s=gap_s,
        )
        log.info("audio: %s %s %s", channel.value, kind, status)
        if self._on_status is None:
            return
        try:
            self._on_status(status)
        except Exception:  # a broken listener must not stop reconnecting
            log.exception("audio status hook failed")
