"""One monotonic clock for every channel, and per-channel chunk timestamping.

Chunk timestamps come from sample counts (smooth, exact spacing) steered towards the
shared host clock (so two devices whose crystals drift apart stay aligned). That is
what keeps mic vs. system-audio drift under 50 ms over an hour (A1).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta

from sombra.contracts import SAMPLE_RATE

NS = 1_000_000_000


class MonotonicClock:
    """Maps ``time.monotonic_ns()`` to local wall-clock time, anchored once at creation.

    Using one anchor for both channels means wall-clock adjustments (NTP) during a
    meeting never make one channel jump relative to the other.
    """

    def __init__(
        self,
        mono_ns: Callable[[], int] = time.monotonic_ns,
        wall: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._mono_ns = mono_ns
        self._anchor_ns = mono_ns()
        self._anchor_wall = wall()

    def now_ns(self) -> int:
        return self._mono_ns()

    def to_wall(self, mono_ns: int) -> datetime:
        return self._anchor_wall + timedelta(microseconds=(mono_ns - self._anchor_ns) / 1000)


class HostSyncedTimer:
    """Timestamps for one capture stream, in host monotonic nanoseconds.

    Call :meth:`observe` from each capture callback with the total input samples
    received so far and the host time at the callback (≈ end of that buffer). The
    timer keeps ``time(sample) = anchor + sample / rate + correction`` and slews
    ``correction`` gently towards the host clock, so callback jitter is smoothed out
    while device-clock drift (tens of ppm) is tracked. A large error (a dropout, the
    OS skipping audio) re-anchors at once. Stamps are strictly increasing.
    """

    def __init__(
        self,
        in_rate: int,
        *,
        gain: float = 0.05,
        max_slew_ns: int = 500_000,
        resync_ns: int = 200_000_000,
    ) -> None:
        self.in_rate = in_rate
        self._gain = gain
        self._max_slew_ns = max_slew_ns
        self._resync_ns = resync_ns
        self._anchor_ns: int | None = None
        self._correction_ns = 0.0
        self._last_stamp_ns: int | None = None
        self.resyncs = 0

    def observe(self, samples_in_total: int, host_ns: int) -> None:
        expected = samples_in_total * NS // self.in_rate
        if self._anchor_ns is None:
            self._anchor_ns = host_ns - expected
            return
        err = (host_ns - self._anchor_ns - expected) - self._correction_ns
        if abs(err) > self._resync_ns:
            self._correction_ns += err
            self.resyncs += 1
            return
        step = max(-self._max_slew_ns, min(self._max_slew_ns, err * self._gain))
        self._correction_ns += step

    def stamp(self, out_index: int) -> int:
        """Host time (ns) of 16 kHz output sample ``out_index``; strictly increasing per call."""
        if self._anchor_ns is None:
            raise RuntimeError("observe() must be called before stamp()")
        t = self._anchor_ns + out_index * NS // SAMPLE_RATE + round(self._correction_ns)
        if self._last_stamp_ns is not None and t <= self._last_stamp_ns:
            t = self._last_stamp_ns + 1
        self._last_stamp_ns = t
        return t
