from datetime import UTC, datetime, timedelta
from itertools import pairwise

import numpy as np

from sombra.audio.buffer import DropOldestQueue
from sombra.audio.capture import ChannelCapture
from sombra.audio.clock import NS, MonotonicClock
from sombra.contracts import SAMPLE_RATE, AudioChunk, Channel

T0 = datetime(2026, 9, 29, 14, 30, tzinfo=UTC)


def _drain(q: DropOldestQueue) -> list[AudioChunk]:
    q.close()
    out = []
    while len(q):
        out.append(q._items.popleft())
    return out


def test_callback_frames_become_stamped_16k_chunks() -> None:
    now = [0]
    clock = MonotonicClock(mono_ns=lambda: now[0], wall=lambda: T0)
    q = DropOldestQueue(1000)
    cap = ChannelCapture(Channel.OTHERS, 48_000, clock, q, chunk_ms=20)
    block = np.full((480, 2), 0.5, dtype=np.float32)  # 10 ms stereo
    for i in range(1, 101):  # 1 s
        now[0] = i * NS // 100 + (i % 3) * 1_000_000  # jittery callbacks
        cap.on_frames(block, overflow=(i == 50))
    chunks = _drain(q)
    assert cap.overflows == 1
    assert len(chunks) >= 48
    assert {c.channel for c in chunks} == {Channel.OTHERS}
    assert all(len(c.pcm_f32le) == 320 * 4 for c in chunks)
    starts = [c.start for c in chunks]
    assert all(b > a for a, b in pairwise(starts))
    # 20 ms apart, give or take the slew.
    gaps = [(b - a).total_seconds() for a, b in pairwise(starts)]
    assert max(abs(g - 0.02) for g in gaps) < 0.002
    # First chunk starts when the first sample was captured (≈ T0).
    assert abs(starts[0] - T0) < timedelta(milliseconds=5)
    pcm = np.frombuffer(chunks[-1].pcm_f32le, "<f4")
    assert np.allclose(pcm, 0.5, atol=1e-3)
    assert SAMPLE_RATE == 16_000


def test_host_time_defaults_to_clock() -> None:
    clock = MonotonicClock(mono_ns=lambda: 7 * NS, wall=lambda: T0)
    q = DropOldestQueue(10)
    cap = ChannelCapture(Channel.ME, 16_000, clock, q, chunk_ms=20)
    cap.on_frames(np.zeros(320, dtype=np.float32))
    [chunk] = _drain(q)
    assert chunk.channel is Channel.ME
    assert chunk.start == T0 - timedelta(milliseconds=20)
