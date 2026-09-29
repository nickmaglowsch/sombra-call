"""``AudioSource`` fake: silent PCM chunks at a fixed real-time pace."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from sombra.contracts import SAMPLE_RATE, AudioChunk, AudioDevice, Channel


class FakeAudioSource:
    """Yields ``count`` chunks (forever if ``None``), alternating ME / OTHERS.

    ``interval_s`` is the real delay between chunks; ``chunk_s`` is the audio
    duration each chunk claims (its ``start`` advances by that much).
    """

    def __init__(
        self,
        *,
        count: int | None = None,
        interval_s: float = 0.0,
        chunk_s: float = 0.5,
        start: datetime | None = None,
    ) -> None:
        self.count = count
        self.interval_s = interval_s
        self.chunk_s = chunk_s
        self.start = start or datetime.now().astimezone()
        self.yielded = 0
        self.streams_opened = 0
        self.closed = False

    def list_devices(self) -> list[AudioDevice]:
        return [
            AudioDevice(id="mic", name="Fake Mic", is_input=True),
            AudioDevice(id="system", name="Fake System Audio", is_input=False),
        ]

    async def stream(self) -> AsyncIterator[AudioChunk]:
        self.streams_opened += 1
        samples = int(SAMPLE_RATE * self.chunk_s)
        while self.count is None or self.yielded < self.count:
            await asyncio.sleep(self.interval_s)
            n = self.yielded
            self.yielded += 1
            yield AudioChunk(
                channel=Channel.ME if n % 2 == 0 else Channel.OTHERS,
                start=self.start + timedelta(seconds=n * self.chunk_s),
                pcm_f32le=bytes(4 * samples),
            )

    async def close(self) -> None:
        self.closed = True
