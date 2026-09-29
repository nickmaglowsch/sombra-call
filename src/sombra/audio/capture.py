"""Per-channel capture pipeline shared by the platform backends.

A backend's audio callback hands raw device frames to :meth:`ChannelCapture.on_frames`;
it resamples, cuts 16 kHz chunks, stamps them from the shared monotonic clock and puts
them on the drop-oldest queue. Pure logic, so it is unit-tested without devices.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from sombra.audio.buffer import DropOldestQueue
from sombra.audio.clock import HostSyncedTimer, MonotonicClock
from sombra.audio.dsp import Chunker
from sombra.contracts import AudioChunk, Channel


class ChannelCapture:
    def __init__(
        self,
        channel: Channel,
        in_rate: int,
        clock: MonotonicClock,
        queue: DropOldestQueue,
        *,
        chunk_ms: int = 50,
    ) -> None:
        self.channel = channel
        self.clock = clock
        self.queue = queue
        self.chunker = Chunker(in_rate, chunk_ms)
        self.timer = HostSyncedTimer(in_rate)
        self.overflows = 0  # device-side overruns reported by the backend

    def on_frames(
        self,
        frames: NDArray[np.floating] | NDArray[np.integer],
        host_ns: int | None = None,
        *,
        overflow: bool = False,
    ) -> None:
        """Audio-thread entry point. ``host_ns`` is the host time at the end of ``frames``."""
        if host_ns is None:
            host_ns = self.clock.now_ns()
        if overflow:
            self.overflows += 1
        chunks = self.chunker.push(frames)
        self.timer.observe(self.chunker.resampler.samples_in, host_ns)
        for index, samples in chunks:
            start = self.clock.to_wall(self.timer.stamp(index))
            self.queue.put(
                AudioChunk(
                    channel=self.channel,
                    start=start,
                    pcm_f32le=samples.astype("<f4", copy=False).tobytes(),
                )
            )
