"""Real-device checks. Run on a Mac (14.4+) by hand: ``uv run pytest -m hardware -s tests/audio``.

Play a YouTube video (system audio) and speak into the mic while the test runs.
"""

import asyncio
import os
import sys
import time

import numpy as np
import pytest

from sombra.contracts import Channel

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.macos,
    pytest.mark.skipif(sys.platform != "darwin", reason="macOS only"),
]

SECONDS = float(os.environ.get("SOMBRA_AUDIO_TEST_SECONDS", "60"))


async def test_capture_both_channels_non_silent() -> None:
    from sombra.audio.macos import MacAudioSource

    src = MacAudioSource(
        mic=os.environ.get("SOMBRA_MIC"), system=os.environ.get("SOMBRA_SYSTEM", "auto")
    )
    rms: dict[Channel, list[float]] = {Channel.ME: [], Channel.OTHERS: []}
    last_start = {ch: None for ch in Channel}
    cpu0, t0 = time.process_time(), time.monotonic()
    gen = src.stream()

    async def consume() -> None:
        async for chunk in gen:
            x = np.frombuffer(chunk.pcm_f32le, "<f4")
            rms[chunk.channel].append(float(np.sqrt(np.mean(x**2))))
            prev = last_start[chunk.channel]
            assert prev is None or chunk.start > prev
            last_start[chunk.channel] = chunk.start
            if time.monotonic() - t0 >= SECONDS:
                return

    await asyncio.wait_for(consume(), SECONDS + 10)
    cpu = (time.process_time() - cpu0) / (time.monotonic() - t0)
    skew = abs((last_start[Channel.ME] - last_start[Channel.OTHERS]).total_seconds())
    await gen.aclose()
    report = {
        "system_path": src.system_path,
        "mic": src.mic_id,
        "chunks": {ch.value: len(v) for ch, v in rms.items()},
        "p95_rms": {ch.value: float(np.percentile(v, 95)) for ch, v in rms.items() if v},
        "dropped": src.dropped_chunks,
        "overflows": src.overflows,
        "process_cpu_pct": round(100 * cpu, 2),
        "last_chunk_skew_s": round(skew, 3),
    }
    print(f"\naudio hardware report: {report}")  # noqa: T201 - the report is the point
    for ch in Channel:
        assert len(rms[ch]) >= SECONDS * 15, f"{ch} produced too few chunks"
        assert np.percentile(rms[ch], 95) > 1e-3, f"{ch} is silent"
    assert cpu < 0.02, f"process CPU {cpu:.1%} over the 2% budget"


RECONNECT_SECONDS = float(os.environ.get("SOMBRA_RECONNECT_TEST_SECONDS", "600"))


async def test_reconnect_during_capture() -> None:
    """A3 manual check. During the run (10 min by default), unplug and replug a headset
    and switch outputs at least 5 times each; both channels must recover every time."""
    from sombra.audio.macos import MacAudioSource
    from sombra.audio.reconnect import SourceStatus

    events: list[SourceStatus] = []
    src = MacAudioSource(
        mic=os.environ.get("SOMBRA_MIC"),
        system=os.environ.get("SOMBRA_SYSTEM", "auto"),
        on_status=events.append,
    )
    counts = {ch: 0 for ch in Channel}
    t0 = time.monotonic()
    gen = src.stream()

    async def consume() -> None:
        async for chunk in gen:
            counts[chunk.channel] += 1
            if time.monotonic() - t0 >= RECONNECT_SECONDS:
                return

    await asyncio.wait_for(consume(), RECONNECT_SECONDS + 10)
    await gen.aclose()
    for e in events:
        print(  # noqa: T201 - the report is the point
            f"{e.at:%H:%M:%S.%f} {e.channel.value:6} {e.kind:9} attempt={e.attempt} "
            f"device={e.device!r} gap={e.gap_s} {e.reason}"
        )
    gaps = {
        ch: [e.gap_s for e in events if e.channel is ch and e.gap_s is not None] for ch in Channel
    }
    print(f"\nreconnect report: chunks={counts} gaps_s={gaps}")  # noqa: T201
    for ch in Channel:
        lost = sum(e.kind == "lost" and e.channel is ch for e in events)
        assert len(gaps[ch]) == lost, f"{ch} lost {lost} times but recovered {len(gaps[ch])}"
        assert all(g < 2.0 for g in gaps[ch]), f"{ch} took over 2 s to recover: {gaps[ch]}"
