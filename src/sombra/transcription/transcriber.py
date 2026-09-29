"""``WhisperTranscriber``: the ``contracts.Transcriber`` implementation (T1, T2, T4).

Per channel there is a two-stage pipeline, each stage on its own single-thread executor::

    chunks ──► [VAD thread: Segmenter] ──► segments ──► [STT thread: whisper] ──► SpeechLine

So VAD keeps up with live audio while whisper is busy, the event loop never runs model
code, and a long EU utterance never delays an OUTROS line (each channel has its own
whisper context). Lines come out in completion order; within a channel they keep
utterance order. An STT error drops that one utterance and is logged; the stream goes on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sombra.contracts import AudioChunk, Channel, SpeechLine, TimelineEntry
from sombra.transcription.filters import HallucinationFilter, build_initial_prompt
from sombra.transcription.models import (
    DEFAULT_WHISPER_MODEL,
    SILERO_VAD,
    default_models_dir,
    whisper_model,
)
from sombra.transcription.vad import (
    Segment,
    Segmenter,
    SpeechDetector,
    VadSettings,
    pcm_from_bytes,
)
from sombra.transcription.whisper import SpeechToText, WhisperSettings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TranscriptionSettings:
    """Everything the orchestrator passes in; the module never reads config files."""

    model: str = DEFAULT_WHISPER_MODEL  # key of models.WHISPER_MODELS
    models_dir: Path | None = None  # default ~/.cache/sombra/models
    language: str = "pt"  # or "auto"
    vocabulary: Sequence[str] = ()  # names, acronyms (T4)
    n_threads: int = 4
    use_gpu: bool = True
    vad: VadSettings = field(default_factory=VadSettings)


@dataclass(frozen=True, slots=True)
class SegmentStat:
    """One finished utterance, for latency metrics (orchestrator logs, benchmarks)."""

    channel: Channel
    speech_end: datetime  # media time the speech ended (wall clock for live capture)
    emitted_at: datetime  # wall clock when the line (or the drop) was decided
    audio_s: float
    stt_s: float  # whisper wall time
    text: str | None  # None when dropped (empty, hallucination, error)

    @property
    def latency_s(self) -> float:
        """End of speech -> SpeechLine, including the VAD's closing silence."""
        return (self.emitted_at - self.speech_end).total_seconds()


DetectorFactory = Callable[[Channel], SpeechDetector]
SttFactory = Callable[[Channel], SpeechToText]


def _now_like(ref: datetime) -> datetime:
    return datetime.now(ref.tzinfo)


class WhisperTranscriber:
    def __init__(
        self,
        *,
        detector_factory: DetectorFactory,
        stt_factory: SttFactory,
        vad: VadSettings | None = None,
        text_filter: HallucinationFilter | None = None,
        on_stat: Callable[[SegmentStat], None] | None = None,
    ) -> None:
        self._detector_factory = detector_factory
        self._stt_factory = stt_factory
        self._vad = vad or VadSettings()
        self._filter = text_filter or HallucinationFilter()
        self._on_stat = on_stat

    @classmethod
    def from_settings(
        cls,
        settings: TranscriptionSettings,
        *,
        on_stat: Callable[[SegmentStat], None] | None = None,
    ) -> WhisperTranscriber:
        """Production wiring: Silero VAD + whisper.cpp from the model cache."""
        from sombra.transcription.silero import SileroVad
        from sombra.transcription.whisper import PyWhisperCppEngine

        models_dir = settings.models_dir or default_models_dir()
        prompt = build_initial_prompt(settings.vocabulary)
        whisper = WhisperSettings(
            model_path=models_dir / whisper_model(settings.model).filename,
            language=settings.language,
            initial_prompt=prompt,
            n_threads=settings.n_threads,
            use_gpu=settings.use_gpu,
        )
        vad_path = models_dir / SILERO_VAD.filename
        return cls(
            detector_factory=lambda _ch: SileroVad(vad_path),
            stt_factory=lambda _ch: PyWhisperCppEngine(whisper),
            vad=settings.vad,
            text_filter=HallucinationFilter(prompt=prompt),
            on_stat=on_stat,
        )

    async def transcribe(self, chunks: AsyncIterator[AudioChunk]) -> AsyncIterator[TimelineEntry]:
        out: asyncio.Queue[SpeechLine | None] = asyncio.Queue()
        driver = asyncio.create_task(self._run(chunks, out))
        try:
            while (line := await out.get()) is not None:
                yield line
        finally:
            if not driver.done():
                driver.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await driver
        await driver  # surface errors from the input stream

    async def _run(
        self, chunks: AsyncIterator[AudioChunk], out: asyncio.Queue[SpeechLine | None]
    ) -> None:
        pipes: dict[Channel, _ChannelPipeline] = {}
        try:
            async for chunk in chunks:
                pipe = pipes.get(chunk.channel)
                if pipe is None:
                    pipe = pipes[chunk.channel] = _ChannelPipeline(chunk.channel, self, out)
                pipe.feed(chunk)
            for pipe in pipes.values():
                pipe.end_of_input()
            await asyncio.gather(*(p.done() for p in pipes.values()))
        finally:
            for pipe in pipes.values():
                pipe.shutdown()
            out.put_nowait(None)


class _ChannelPipeline:
    def __init__(
        self, channel: Channel, owner: WhisperTranscriber, out: asyncio.Queue[SpeechLine | None]
    ) -> None:
        self.channel = channel
        self._owner = owner
        self._out = out
        self._chunks: asyncio.Queue[AudioChunk | None] = asyncio.Queue()
        self._segments: asyncio.Queue[Segment | None] = asyncio.Queue()
        name = channel.name.lower()
        self._vad_ex = ThreadPoolExecutor(1, thread_name_prefix=f"sombra-vad-{name}")
        self._stt_ex = ThreadPoolExecutor(1, thread_name_prefix=f"sombra-stt-{name}")
        # the model loads on its own thread while VAD already runs
        loop = asyncio.get_running_loop()
        self._stt_ready = loop.run_in_executor(self._stt_ex, owner._stt_factory, channel)
        self._tasks = [
            asyncio.create_task(self._segment_loop()),
            asyncio.create_task(self._stt_loop()),
        ]

    def feed(self, chunk: AudioChunk) -> None:
        self._chunks.put_nowait(chunk)  # unbounded: audio is never dropped

    def end_of_input(self) -> None:
        self._chunks.put_nowait(None)

    async def done(self) -> None:
        await asyncio.gather(*self._tasks)

    def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._stt_ready.cancel()
        self._vad_ex.shutdown(wait=False, cancel_futures=True)
        self._stt_ex.shutdown(wait=False, cancel_futures=True)

    async def _segment_loop(self) -> None:
        loop = asyncio.get_running_loop()
        detector = await loop.run_in_executor(
            self._vad_ex, self._owner._detector_factory, self.channel
        )
        segmenter = Segmenter(detector, self._owner._vad)
        while True:
            chunk = await self._chunks.get()
            if chunk is None:
                closed = await loop.run_in_executor(self._vad_ex, segmenter.flush)
            else:
                pcm = pcm_from_bytes(chunk.pcm_f32le)
                closed = await loop.run_in_executor(self._vad_ex, segmenter.feed, chunk.start, pcm)
            for seg in closed:
                self._segments.put_nowait(seg)
            if chunk is None:
                self._segments.put_nowait(None)
                return

    async def _stt_loop(self) -> None:
        loop = asyncio.get_running_loop()
        stt = await self._stt_ready
        while (seg := await self._segments.get()) is not None:
            t0 = time.perf_counter()
            text: str | None
            try:
                raw = await loop.run_in_executor(self._stt_ex, stt.transcribe, seg.audio)
            except Exception:
                log.exception("STT failed on a %.1f s %s utterance", seg.duration_s, self.channel)
                text = None
            else:
                text = self._owner._filter.clean(raw)
                if text is None and raw.strip():
                    log.debug("dropped hallucination on %s: %r", self.channel, raw)
            stt_s = time.perf_counter() - t0
            if text is not None:
                self._out.put_nowait(SpeechLine(ts=seg.start, channel=self.channel, text=text))
            if self._owner._on_stat is not None:
                self._owner._on_stat(
                    SegmentStat(
                        channel=self.channel,
                        speech_end=seg.end,
                        emitted_at=_now_like(seg.end),
                        audio_s=seg.duration_s,
                        stt_s=stt_s,
                        text=text,
                    )
                )
