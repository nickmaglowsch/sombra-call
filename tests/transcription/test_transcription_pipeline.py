"""WhisperTranscriber with fake VAD and STT: channels, ordering, drops, failures."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from transcription_fakes import (
    T0,
    AmplitudeDetector,
    ScriptedStt,
    aiter_of,
    chunks,
    concat,
    interleave,
    silence,
    tone,
)

from sombra.contracts import AudioChunk, Channel, SpeechLine, TimelineEntry, Transcriber
from sombra.transcription import (
    SegmentStat,
    TranscriptionSettings,
    VadSettings,
    WhisperTranscriber,
)

TWO_UTTERANCES = concat(silence(0.5), tone(1.0), silence(1.0), tone(1.0), silence(1.0))


def make(
    stts: dict[Channel, ScriptedStt],
    stats: list[SegmentStat] | None = None,
    detectors: dict[Channel, AmplitudeDetector] | None = None,
) -> WhisperTranscriber:
    dets = detectors if detectors is not None else {}

    def detector(ch: Channel) -> AmplitudeDetector:
        return dets.setdefault(ch, AmplitudeDetector())

    return WhisperTranscriber(
        detector_factory=detector,
        stt_factory=lambda ch: stts[ch],
        on_stat=stats.append if stats is not None else None,
    )


async def collect(t: Transcriber, items: list[AudioChunk]) -> list[TimelineEntry]:
    return [line async for line in t.transcribe(aiter_of(items))]


def speech(lines: list[TimelineEntry]) -> list[SpeechLine]:
    assert all(isinstance(x, SpeechLine) for x in lines)
    return [x for x in lines if isinstance(x, SpeechLine)]


async def test_emits_one_line_per_utterance_with_segment_start() -> None:
    t = make({Channel.ME: ScriptedStt(["bom dia", "vamos começar"])})
    lines = speech(await collect(t, chunks(TWO_UTTERANCES)))
    assert [(x.channel, x.text) for x in lines] == [
        (Channel.ME, "bom dia"),
        (Channel.ME, "vamos começar"),
    ]
    assert abs(lines[0].ts - (T0 + timedelta(seconds=0.5))) < timedelta(milliseconds=40)
    assert abs(lines[1].ts - (T0 + timedelta(seconds=2.5))) < timedelta(milliseconds=40)
    assert lines[0].to_line() == "[14:30:00] EU: bom dia"


async def test_channels_are_tagged_and_isolated() -> None:
    stts = {Channel.ME: ScriptedStt(["eu falei"]), Channel.OTHERS: ScriptedStt(["outro falou"])}
    audio = concat(tone(1.0), silence(1.0))
    items = interleave(chunks(audio, Channel.ME), chunks(audio, Channel.OTHERS))
    lines = speech(await collect(make(stts), items))
    assert sorted((x.channel.value, x.text) for x in lines) == [
        ("EU", "eu falei"),
        ("OUTROS", "outro falou"),
    ]
    # each channel has its own STT thread
    assert stts[Channel.ME].threads.isdisjoint(stts[Channel.OTHERS].threads)


async def test_slow_channel_never_blocks_the_other() -> None:
    gate = threading.Event()
    stts = {
        Channel.OTHERS: ScriptedStt(["fala longa"], gate=gate),
        Channel.ME: ScriptedStt(["resposta rápida"]),
    }
    others = chunks(concat(tone(1.0), silence(1.0)), Channel.OTHERS)
    me = chunks(concat(tone(1.0), silence(1.0)), Channel.ME, start=T0 + timedelta(seconds=3))

    async def feed() -> AsyncIterator[AudioChunk]:
        for c in others:
            yield c
        # wait until OTHERS is stuck inside whisper
        assert await asyncio.to_thread(stts[Channel.OTHERS].entered.wait, 5)
        for c in me:
            yield c

    stream = make(stts).transcribe(feed())
    try:
        first = await asyncio.wait_for(anext(stream), 5)
        assert isinstance(first, SpeechLine) and first.channel is Channel.ME
        assert not gate.is_set()
        gate.set()
        second = await asyncio.wait_for(anext(stream), 5)
        assert isinstance(second, SpeechLine) and second.text == "fala longa"
    finally:
        gate.set()
        await stream.aclose()


async def test_order_within_a_channel_survives_variable_stt_time() -> None:
    stt = ScriptedStt(["primeira", "segunda", "terceira"], delay_s=lambda i: 0.2 if i == 0 else 0)
    audio = concat(*(concat(tone(0.8), silence(0.8)) for _ in range(3)))
    lines = speech(await collect(make({Channel.ME: stt}), chunks(audio)))
    assert [x.text for x in lines] == ["primeira", "segunda", "terceira"]
    assert [x.ts for x in lines] == sorted(x.ts for x in lines)


async def test_event_loop_keeps_running_during_stt() -> None:
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        stt = ScriptedStt(["demorado"], delay_s=0.3)
        await collect(make({Channel.ME: stt}), chunks(concat(tone(1.0), silence(1.0))))
    finally:
        task.cancel()
    assert ticks >= 10


async def test_hallucinations_and_empty_outputs_are_dropped() -> None:
    stats: list[SegmentStat] = []
    stt = ScriptedStt(["Legendas pela comunidade Amara.org", "   ", "texto real"])
    audio = concat(*(concat(tone(0.8), silence(0.8)) for _ in range(3)))
    lines = speech(await collect(make({Channel.ME: stt}, stats), chunks(audio)))
    assert [x.text for x in lines] == ["texto real"]
    assert [s.text for s in stats] == [None, None, "texto real"]


async def test_stt_error_drops_only_that_utterance(caplog: pytest.LogCaptureFixture) -> None:
    class Flaky(ScriptedStt):
        def transcribe(self, audio: object) -> str:
            text = super().transcribe(audio)  # type: ignore[arg-type]
            if self.calls == 1:
                raise RuntimeError("whisper exploded")
            return text

    with caplog.at_level(logging.ERROR, logger="sombra.transcription.transcriber"):
        lines = speech(
            await collect(
                make({Channel.ME: Flaky(["x", "depois do erro"])}), chunks(TWO_UTTERANCES)
            )
        )
    assert [x.text for x in lines] == ["depois do erro"]
    assert "STT failed" in caplog.text


async def test_silence_produces_no_lines_and_no_model_work() -> None:
    stt = ScriptedStt()
    dets: dict[Channel, AmplitudeDetector] = {}
    lines = await collect(make({Channel.ME: stt}, detectors=dets), chunks(silence(60.0)))
    assert lines == []
    assert stt.calls == 0
    assert dets[Channel.ME].calls == 0  # the energy gate kept the VAD model idle too


async def test_stats_report_latency_from_end_of_speech() -> None:
    stats: list[SegmentStat] = []
    await collect(make({Channel.ME: ScriptedStt(["oi"])}, stats), chunks(concat(tone(1.0))))
    [s] = stats
    assert s.channel is Channel.ME and s.text == "oi"
    assert abs(s.speech_end - (T0 + timedelta(seconds=1.0))) < timedelta(milliseconds=40)
    assert s.audio_s > 0.95 and s.stt_s >= 0
    assert s.latency_s == (s.emitted_at - s.speech_end).total_seconds()


async def test_input_errors_propagate() -> None:
    async def broken() -> AsyncIterator[AudioChunk]:
        for c in chunks(tone(0.5)):
            yield c
        raise OSError("device gone")

    t = make({Channel.ME: ScriptedStt()})
    with pytest.raises(OSError, match="device gone"):
        async for _ in t.transcribe(broken()):
            pass


async def test_consumer_can_stop_early() -> None:
    t = make({Channel.ME: ScriptedStt(["um", "dois"])})
    stream = t.transcribe(aiter_of(chunks(TWO_UTTERANCES)))
    first = await anext(stream)
    assert isinstance(first, SpeechLine) and first.text == "um"
    await stream.aclose()


async def test_custom_vad_settings_are_used() -> None:
    t = WhisperTranscriber(
        detector_factory=lambda _ch: AmplitudeDetector(),
        stt_factory=lambda _ch: ScriptedStt(),
        vad=VadSettings(min_speech_ms=2000),
    )
    assert await collect(t, chunks(TWO_UTTERANCES)) == []  # 1 s utterances are now too short


async def test_from_settings_needs_downloaded_models(tmp_path: Path) -> None:
    t = WhisperTranscriber.from_settings(
        TranscriptionSettings(model="tiny", models_dir=tmp_path, vocabulary=["Nick"])
    )
    with pytest.raises(FileNotFoundError, match="download_models"):
        await collect(t, chunks(tone(0.5)))


def test_from_settings_rejects_unknown_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown whisper model"):
        WhisperTranscriber.from_settings(TranscriptionSettings(model="huge", models_dir=tmp_path))


# --- failures on a live (never-ending) stream must surface, not hang -------------------


async def endless(channel: Channel = Channel.ME) -> AsyncIterator[AudioChunk]:
    """Live-capture style: an utterance every 2 s, forever."""
    block = concat(tone(1.0), silence(1.0))
    start = T0
    while True:
        for c in chunks(block, channel, start=start):
            yield c
            await asyncio.sleep(0.001)
        start += timedelta(seconds=2)


async def test_failing_stt_factory_surfaces_on_a_live_stream() -> None:
    def broken(_ch: Channel) -> ScriptedStt:
        raise FileNotFoundError("model missing")

    t = WhisperTranscriber(detector_factory=lambda _ch: AmplitudeDetector(), stt_factory=broken)
    with pytest.raises(FileNotFoundError, match="model missing"):
        async with asyncio.timeout(5):
            async for _ in t.transcribe(endless()):
                pass


async def test_failing_detector_surfaces_on_a_live_stream() -> None:
    class Dies(AmplitudeDetector):
        def __call__(self, frame: object) -> float:
            if self.calls >= 20:
                raise RuntimeError("onnxruntime error")
            return super().__call__(frame)  # type: ignore[arg-type]

    t = WhisperTranscriber(
        detector_factory=lambda _ch: Dies(), stt_factory=lambda _ch: ScriptedStt()
    )
    with pytest.raises(RuntimeError, match="onnxruntime error"):
        async with asyncio.timeout(5):
            async for _ in t.transcribe(endless()):
                pass


async def test_failing_on_stat_does_not_stop_transcription(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def bad_hook(_stat: SegmentStat) -> None:
        raise ValueError("logging broke")

    t = WhisperTranscriber(
        detector_factory=lambda _ch: AmplitudeDetector(),
        stt_factory=lambda _ch: ScriptedStt(["um", "dois", "três"]),
        on_stat=bad_hook,
    )
    got: list[str] = []
    stream = t.transcribe(endless())
    try:
        async with asyncio.timeout(5):
            while len(got) < 3:
                line = await anext(stream)
                assert isinstance(line, SpeechLine)
                got.append(line.text)
    finally:
        await stream.aclose()
    assert got == ["um", "dois", "três"]
    assert "on_stat callback failed" in caplog.text


async def test_cancelling_a_live_stream_shuts_down_cleanly() -> None:
    t = make({Channel.ME: ScriptedStt()})
    stream = t.transcribe(endless())
    first = await asyncio.wait_for(anext(stream), 5)
    assert isinstance(first, SpeechLine)
    await stream.aclose()
