import random
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from sombra.audio.clock import NS, HostSyncedTimer, MonotonicClock
from sombra.contracts import SAMPLE_RATE


def test_monotonic_clock_maps_to_wall_from_one_anchor() -> None:
    now = [5 * NS]
    wall0 = datetime(2026, 9, 29, 14, 30, tzinfo=UTC)
    clock = MonotonicClock(mono_ns=lambda: now[0], wall=lambda: wall0)
    now[0] += 1_500_000_000
    assert clock.now_ns() == 6_500_000_000
    assert clock.to_wall(clock.now_ns()) == wall0 + timedelta(seconds=1.5)


def test_default_clock_is_local_aware() -> None:
    clock = MonotonicClock()
    assert clock.to_wall(clock.now_ns()).tzinfo is not None


def test_stamp_before_observe_fails() -> None:
    with pytest.raises(RuntimeError):
        HostSyncedTimer(48_000).stamp(0)


def _simulate(
    ppm: float, seconds: float, *, rate: int = 48_000, block: int = 480, seed: int = 0
) -> tuple[list[int], list[int]]:
    """Device whose clock runs ``ppm`` fast; callbacks jitter by up to 3 ms.

    Returns (stamped chunk starts, true host times of those chunk starts).
    """
    rng = random.Random(seed)
    timer = HostSyncedTimer(rate)
    t0 = 1_000 * NS
    true_rate = rate * (1 + ppm / 1e6)  # device samples per host second
    stamps, truth = [], []
    n_in = 0
    chunk = SAMPLE_RATE // 20  # 50 ms chunks
    next_chunk = 0
    for _ in range(int(seconds * rate / block)):
        n_in += block
        host_end = t0 + round(n_in / true_rate * NS)
        timer.observe(n_in, host_end + rng.randint(0, 3_000_000))
        produced = n_in * SAMPLE_RATE // rate
        while next_chunk + chunk <= produced:
            stamps.append(timer.stamp(next_chunk))
            truth.append(t0 + round(next_chunk * rate / SAMPLE_RATE / true_rate * NS))
            next_chunk += chunk
    return stamps, truth


@pytest.mark.slow
def test_one_hour_drift_between_channels_stays_under_50ms() -> None:
    # Mic 80 ppm fast, system audio 60 ppm slow: 0.5 s apart after 1 h if stamped by
    # sample count alone. Both must stay within 50 ms of the shared host clock.
    me, me_true = _simulate(+80, 3600, seed=1)
    others, others_true = _simulate(-60, 3600, rate=44_100, block=441, seed=2)
    me_err = [s - t for s, t in zip(me, me_true, strict=True)]
    others_err = [s - t for s, t in zip(others, others_true, strict=True)]
    assert max(map(abs, me_err[100:])) < 50_000_000
    assert max(map(abs, others_err[100:])) < 50_000_000
    # Chunk i of each channel covers the same host instant (both 50 ms grids).
    n = min(len(me), len(others))
    inter = [
        abs((a - ta) - (b - tb))
        for a, ta, b, tb in zip(me[:n], me_true[:n], others[:n], others_true[:n], strict=True)
    ]
    assert max(inter[100:]) < 50_000_000


def test_stamps_strictly_increase() -> None:
    stamps, _ = _simulate(+200, 60)
    assert all(b > a for a, b in pairwise(stamps))


def test_large_gap_resyncs_immediately() -> None:
    timer = HostSyncedTimer(16_000)
    timer.observe(1600, 100 * NS)
    before = timer.stamp(1600)
    # The OS dropped 2 s of audio: the host clock jumps ahead of the sample count.
    timer.observe(3200, 100 * NS + 2_100_000_000)
    assert timer.resyncs == 1
    assert timer.stamp(3200) - before == pytest.approx(2_100_000_000, abs=1_000_000)


def test_backwards_correction_is_clamped_to_monotonic() -> None:
    timer = HostSyncedTimer(16_000, resync_ns=1)
    timer.observe(1600, 10 * NS)
    a = timer.stamp(1600)
    timer.observe(3200, 10 * NS - NS)  # host says we are far behind
    assert timer.stamp(1600) > a
