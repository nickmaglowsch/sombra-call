"""``Transcriber`` fake: one scripted line per ``chunks_per_line`` audio chunks."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from sombra.contracts import AudioChunk, TimelineEntry


class FakeTranscriber:
    """Emits the scripted entries in order as audio arrives, then keeps consuming.

    Tying output to input means pausing audio also pauses the transcript, like the
    real VAD + whisper pipeline. ``fail_after`` raises once that many lines have
    been emitted (to exercise crash isolation).
    """

    def __init__(
        self,
        script: Iterable[TimelineEntry],
        *,
        chunks_per_line: int = 1,
        fail_after: int | None = None,
    ) -> None:
        self.script = list(script)
        self.chunks_per_line = chunks_per_line
        self.fail_after = fail_after
        self.emitted = 0
        self.chunks_seen = 0
        self.calls = 0

    async def transcribe(self, chunks: AsyncIterator[AudioChunk]) -> AsyncIterator[TimelineEntry]:
        self.calls += 1
        async for _ in chunks:
            self.chunks_seen += 1
            if self.chunks_seen % self.chunks_per_line or self.emitted >= len(self.script):
                continue
            if self.fail_after is not None and self.emitted == self.fail_after:
                self.fail_after = None
                raise RuntimeError("fake transcriber crashed")
            entry = self.script[self.emitted]
            self.emitted += 1
            yield entry

    @property
    def exhausted(self) -> bool:
        return self.emitted >= len(self.script)
