"""``FileAudioSource``: replay two WAV files (me / others) as an ``AudioSource``.

Used by transcription tests and the offline replay harness. Files go through the same
downmix + resample + chunking as live capture; timestamps are exact
(``start + sample_index / 16 kHz``), so runs are reproducible.
"""

from __future__ import annotations

import asyncio
import time
import wave
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from sombra.audio.dsp import Chunker
from sombra.contracts import SAMPLE_RATE, AudioChunk, AudioDevice, Channel

_READ_FRAMES = 4096


def read_wav_blocks(path: Path) -> tuple[int, Iterator[NDArray[np.float32]]]:
    """Sample rate and an iterator of ``(n, channels)`` float32 blocks in [-1, 1)."""
    w = wave.open(str(path), "rb")  # noqa: SIM115 - closed by the generator below
    rate, nch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
    if width not in (1, 2, 3, 4):
        w.close()
        raise ValueError(f"{path}: unsupported sample width {width}")

    def blocks() -> Iterator[NDArray[np.float32]]:
        with w:
            while data := w.readframes(_READ_FRAMES):
                yield _decode(data, width).reshape(-1, nch)

    return rate, blocks()


def _decode(data: bytes, width: int) -> NDArray[np.float32]:
    if width == 1:  # unsigned 8-bit
        return ((np.frombuffer(data, np.uint8).astype(np.float32) - 128) / 128).astype(np.float32)
    if width == 3:  # little-endian 24-bit: widen to 32-bit
        b = np.frombuffer(data, np.uint8).reshape(-1, 3)
        i32 = (b[:, 0].astype(np.int32) << 8) | (b[:, 1].astype(np.int32) << 16)
        i32 |= b[:, 2].astype(np.int8).astype(np.int32) << 24
        return (i32.astype(np.float32) / 2**31).astype(np.float32)
    dtype = {2: "<i2", 4: "<i4"}[width]
    scale = np.float32(2 ** (8 * width - 1))
    return (np.frombuffer(data, dtype).astype(np.float32) / scale).astype(np.float32)


class FileAudioSource:
    """Two WAV files as mic (``Channel.ME``) and system audio (``Channel.OTHERS``).

    Args:
        me, others: WAV paths (PCM 8/16/24/32-bit, any rate/channels); either may be None.
        start: wall-clock time of sample 0 (default: when :meth:`stream` starts).
        realtime: pace chunks at wall-clock speed (each chunk is yielded once it would
            have finished recording) instead of as fast as possible.
    """

    def __init__(
        self,
        me: Path | None,
        others: Path | None,
        *,
        start: datetime | None = None,
        realtime: bool = False,
        chunk_ms: int = 50,
    ) -> None:
        if me is None and others is None:
            raise ValueError("need at least one WAV file")
        self.paths = {Channel.ME: me, Channel.OTHERS: others}
        self.start = start
        self.realtime = realtime
        self.chunk_ms = chunk_ms
        self._closed = False

    def list_devices(self) -> list[AudioDevice]:
        return [
            AudioDevice(id=f"file:{p}", name=p.name, is_input=ch is Channel.ME)
            for ch, p in self.paths.items()
            if p is not None
        ]

    async def close(self) -> None:
        self._closed = True

    async def stream(self) -> AsyncIterator[AudioChunk]:
        start = self.start or datetime.now().astimezone()
        t0 = time.monotonic()
        its = [self._chunks(ch, p, start) for ch, p in self.paths.items() if p is not None]
        heads: list[AudioChunk | None] = [next(it, None) for it in its]
        while not self._closed:
            live = [(c.start, i) for i, c in enumerate(heads) if c is not None]
            if not live:
                return
            _, i = min(live)  # earliest first; ME before OTHERS on ties
            chunk = heads[i]
            assert chunk is not None  # noqa: S101 - narrowed by `live`
            if self.realtime:
                n = len(chunk.pcm_f32le) // 4
                due = (chunk.start - start).total_seconds() + n / SAMPLE_RATE
                await asyncio.sleep(max(0.0, due - (time.monotonic() - t0)))
            else:
                await asyncio.sleep(0)  # let other tasks run between chunks
            yield chunk
            heads[i] = next(its[i], None)

    def _chunks(self, channel: Channel, path: Path, start: datetime) -> Iterator[AudioChunk]:
        rate, blocks = read_wav_blocks(path)
        chunker = Chunker(rate, self.chunk_ms)

        def make(index: int, samples: NDArray[np.float32]) -> AudioChunk:
            return AudioChunk(
                channel=channel,
                start=start + timedelta(microseconds=index * 1_000_000 / SAMPLE_RATE),
                pcm_f32le=samples.astype("<f4", copy=False).tobytes(),
            )

        for block in blocks:
            for index, samples in chunker.push(block):
                yield make(index, samples)
        for index, samples in chunker.flush():
            yield make(index, samples)
