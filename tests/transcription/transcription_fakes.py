"""Fakes and signal helpers shared by the transcription tests (no models, no devices)."""

from __future__ import annotations

import threading
import time
import wave
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from sombra.contracts import SAMPLE_RATE, AudioChunk, Channel

T0 = datetime(2026, 9, 29, 14, 30, 0)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "transcription" / "ptbr_reuniao.wav"


def tone(seconds: float, amp: float = 0.3, hz: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def silence(seconds: float, level: float = 1e-4, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * SAMPLE_RATE)) * level).astype(np.float32)


def concat(*parts: np.ndarray) -> np.ndarray:
    return np.concatenate(parts).astype(np.float32)


def read_wav(path: Path = FIXTURE) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SAMPLE_RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        raw = w.readframes(w.getnframes())
    return (np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0).astype(np.float32)


def chunks(
    pcm: np.ndarray, channel: Channel = Channel.ME, start: datetime = T0, chunk_s: float = 0.1
) -> list[AudioChunk]:
    n = int(chunk_s * SAMPLE_RATE)
    return [
        AudioChunk(
            channel=channel,
            start=start + timedelta(seconds=i / SAMPLE_RATE),
            pcm_f32le=pcm[i : i + n].astype("<f4").tobytes(),
        )
        for i in range(0, len(pcm), n)
    ]


def interleave(*streams: list[AudioChunk]) -> list[AudioChunk]:
    """Merge per-channel chunk lists by start time, as a capture module would."""
    return sorted((c for s in streams for c in s), key=lambda c: c.start)


async def aiter_of(items: Iterable[AudioChunk]) -> AsyncIterator[AudioChunk]:
    for item in items:
        yield item


class AmplitudeDetector:
    """Speech probability from loudness: anything above ``level`` peak is speech."""

    def __init__(self, level: float = 0.05) -> None:
        self.level = level
        self.calls = 0
        self.resets = 0

    def __call__(self, frame: np.ndarray) -> float:
        self.calls += 1
        return 0.9 if float(np.abs(frame).max()) > self.level else 0.02

    def reset(self) -> None:
        self.resets += 1


class ScriptedStt:
    """Returns scripted texts in order; ``fn`` can compute text from the audio instead."""

    def __init__(
        self,
        texts: Iterable[str] = (),
        *,
        delay_s: Callable[[int], float] | float = 0.0,
        gate: threading.Event | None = None,
        fn: Callable[[np.ndarray], str] | None = None,
    ) -> None:
        self.texts = list(texts)
        self.delay_s = delay_s
        self.gate = gate
        self.fn = fn
        self.calls = 0
        self.entered = threading.Event()
        self.threads: set[str] = set()

    def transcribe(self, audio: np.ndarray) -> str:
        i = self.calls
        self.calls += 1
        self.entered.set()
        self.threads.add(threading.current_thread().name)
        if self.gate is not None:
            assert self.gate.wait(5), "gate never opened"
        delay = self.delay_s(i) if callable(self.delay_s) else self.delay_s
        if delay:
            time.sleep(delay)
        if self.fn is not None:
            return self.fn(audio)
        return self.texts[i] if i < len(self.texts) else f"frase {i}"
