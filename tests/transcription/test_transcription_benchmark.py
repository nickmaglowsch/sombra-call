"""Latency benchmark on real hardware: end of speech -> SpeechLine with large-v3-turbo q5.

Needs the default model downloaded (``uv run python scripts/download_models.py``) and is
meant for Apple Silicon (Metal). Run by hand and paste the numbers into the PR::

    uv run pytest -m hardware tests/transcription/test_transcription_benchmark.py -s
"""

from __future__ import annotations

import asyncio
import logging
import resource
import sys
import statistics
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest
from transcription_fakes import read_wav

from sombra.contracts import SAMPLE_RATE, AudioChunk, Channel
from sombra.transcription import (
    DEFAULT_WHISPER_MODEL,
    SegmentStat,
    TranscriptionSettings,
    WhisperTranscriber,
    default_models_dir,
    whisper_model,
)

log = logging.getLogger(__name__)
ROUNDS = 5  # fixture has 2 utterances -> 10 samples per channel


async def realtime(
    channel: Channel, rounds: int, chunk_s: float = 0.1
) -> AsyncIterator[AudioChunk]:
    """Play the fixture at wall-clock pace, stamping chunks like live capture does."""
    pcm = read_wav()
    n = int(chunk_s * SAMPLE_RATE)
    for _ in range(rounds):
        for i in range(0, len(pcm), n):
            now = datetime.now().astimezone()
            yield AudioChunk(channel, now, pcm[i : i + n].astype("<f4").tobytes())
            await asyncio.sleep(chunk_s)


async def _merge(*streams: AsyncIterator[AudioChunk]) -> AsyncIterator[AudioChunk]:
    queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue()

    async def pump(s: AsyncIterator[AudioChunk]) -> None:
        async for c in s:
            await queue.put(c)
        await queue.put(None)

    tasks = [asyncio.create_task(pump(s)) for s in streams]
    remaining = len(tasks)
    while remaining:
        item = await queue.get()
        if item is None:
            remaining -= 1
        else:
            yield item


@pytest.mark.hardware
@pytest.mark.macos
async def test_end_of_speech_to_line_p50_under_one_second() -> None:
    models_dir = default_models_dir()
    if not (models_dir / whisper_model(DEFAULT_WHISPER_MODEL).filename).is_file():
        pytest.fail("run scripts/download_models.py first")
    stats: list[SegmentStat] = []
    t = WhisperTranscriber.from_settings(TranscriptionSettings(), on_stat=stats.append)
    # both channels at once: the realistic worst case (two whisper contexts on one GPU)
    stream = _merge(realtime(Channel.ME, ROUNDS), realtime(Channel.OTHERS, ROUNDS))
    lines = [x async for x in t.transcribe(stream)]
    assert lines
    lat = sorted(s.latency_s for s in stats if s.text)
    stt = sorted(s.stt_s for s in stats if s.text)
    p50 = statistics.median(lat)
    p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))]
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # bytes on macOS, KiB on Linux
    peak_mb = peak / 1e6 if sys.platform == "darwin" else peak / 1e3
    summary = (
        f"n={len(lat)} end-of-speech->line p50={p50:.3f}s p95={p95:.3f}s "
        f"(whisper only p50={statistics.median(stt):.3f}s), peak RSS {peak_mb:.0f} MB"
    )
    log.warning(summary)
    assert p50 <= 1.0, summary
    assert timedelta(seconds=p95) < timedelta(seconds=3), summary
