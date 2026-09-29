"""Real Silero VAD + whisper.cpp ``tiny`` on the synthetic PT-BR fixture.

Runs in CI (models are cached in ~/.cache/sombra/models by the workflow). Outside CI, a
machine that cannot reach the model hosts skips these instead of failing; in CI an
unreachable host is a failure.
"""

from __future__ import annotations

import os
import time
import urllib.error
from pathlib import Path

import pytest
from transcription_fakes import aiter_of, chunks, read_wav, silence

from sombra.contracts import Channel, SpeechLine
from sombra.transcription import (
    SILERO_VAD,
    Segmenter,
    TranscriptionSettings,
    WhisperTranscriber,
    ensure_model,
    whisper_model,
)
from sombra.transcription.filters import normalize
from sombra.transcription.silero import SileroVad
from sombra.transcription.vad import pcm_from_bytes

# eSpeak's robotic voice + whisper ``tiny`` only gets the common words right (CI heard
# "bom dia todos vamos tomer a raja ..."), so this checks wiring and PT decoding, not
# accuracy; accuracy is the large model's job (hardware benchmark, spike #15).
KEY_WORDS = ("bom dia", "todos", "vamos")


def _ensure(name: str | None) -> Path:
    model = SILERO_VAD if name is None else whisper_model(name)
    try:
        return ensure_model(model)
    except (urllib.error.URLError, OSError) as e:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"cannot download {model.filename} here ({e}); CI runs this test")


@pytest.fixture(scope="module")
def silero_path() -> Path:
    return _ensure(None)


@pytest.fixture(scope="module")
def tiny_dir(silero_path: Path) -> Path:
    return _ensure("tiny").parent


def test_silero_finds_the_two_sentences(silero_path: Path) -> None:
    seg = Segmenter(SileroVad(silero_path))
    out = []
    for c in chunks(read_wav()):
        out += seg.feed(c.start, pcm_from_bytes(c.pcm_f32le))
    out += seg.flush()
    assert len(out) == 2
    t0 = chunks(read_wav())[0].start
    starts = [(s.start - t0).total_seconds() for s in out]
    assert 0.8 <= starts[0] <= 1.3
    assert 6.5 <= starts[1] <= 7.5


@pytest.mark.slow
async def test_tiny_model_transcribes_ptbr_key_words(tiny_dir: Path) -> None:
    t = WhisperTranscriber.from_settings(
        TranscriptionSettings(model="tiny", models_dir=tiny_dir, n_threads=2, use_gpu=False)
    )
    lines = [x async for x in t.transcribe(aiter_of(chunks(read_wav(), Channel.OTHERS)))]
    assert all(isinstance(x, SpeechLine) and x.channel is Channel.OTHERS for x in lines)
    text = normalize(" ".join(x.text for x in lines if isinstance(x, SpeechLine)))
    assert len(lines) == 2, f"expected one line per sentence, tiny heard: {text!r}"
    missing = [w for w in KEY_WORDS if w not in text]
    assert not missing, f"missing {missing}; tiny heard: {text!r}"


@pytest.mark.slow
@pytest.mark.parametrize("level", [1e-4, 3e-3], ids=["-80dBFS-gated", "-50dBFS-silero"])
def test_ten_minutes_of_silence_costs_under_one_percent_cpu(
    silero_path: Path, level: float
) -> None:
    """Silence through the real VAD: no segments, < 1% of one core.

    -80 dBFS is below the energy gate (the VAD model never runs); -50 dBFS is a noisy
    room, so Silero scores every 32 ms frame.
    """
    seg = Segmenter(SileroVad(silero_path))
    t0 = chunks(silence(0.1))[0].start
    audio = silence(600.0, level=level, seed=7)
    step = 1600  # 100 ms chunks, like live capture
    cpu = time.process_time()
    closed = []
    for i in range(0, len(audio), step):
        closed += seg.feed(t0, audio[i : i + step])
    closed += seg.flush()
    cpu = time.process_time() - cpu
    assert closed == []
    assert cpu / 600.0 < 0.01, f"{cpu:.2f} s CPU for 600 s of silence"
