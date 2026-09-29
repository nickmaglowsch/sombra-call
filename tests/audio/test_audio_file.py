import time
import wave
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest

from sombra.audio import FileAudioSource
from sombra.audio.file import read_wav_blocks
from sombra.contracts import SAMPLE_RATE, AudioChunk, AudioSource, Channel

T0 = datetime(2026, 9, 29, 14, 30, tzinfo=UTC)


def _write_wav(path: Path, x: np.ndarray, rate: int, width: int = 2) -> Path:
    """Write float samples ``(n,)`` or ``(n, ch)`` in [-1, 1) as integer PCM."""
    x2 = x.reshape(len(x), -1)
    if width == 1:
        data = (x2 * 128 + 128).astype(np.uint8).tobytes()
    elif width == 3:
        i32 = (x2 * 2**31).astype("<i4")
        data = i32.view(np.uint8).reshape(-1, 4)[:, 1:].tobytes()
    else:
        data = (x2 * 2 ** (8 * width - 1)).astype(f"<i{width}").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(x2.shape[1])
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(data)
    return path


async def _collect(src: FileAudioSource) -> list[AudioChunk]:
    return [c async for c in src.stream()]


async def test_interleaves_both_files_with_channels_and_timestamps(tmp_path: Path) -> None:
    me = _write_wav(tmp_path / "me.wav", np.full(16_000, 0.25), 16_000)  # 1.0 s mono 16 kHz
    others = _write_wav(
        tmp_path / "others.wav", np.full((36_000, 2), -0.5), 48_000
    )  # 0.75 s stereo 48 kHz
    src: AudioSource = FileAudioSource(me, others, start=T0, chunk_ms=50)
    chunks = await _collect(src)  # type: ignore[arg-type]

    me_c = [c for c in chunks if c.channel is Channel.ME]
    ot_c = [c for c in chunks if c.channel is Channel.OTHERS]
    assert len(me_c) == 20 and len(ot_c) == 15
    assert [c.start for c in me_c] == [T0 + timedelta(milliseconds=50 * i) for i in range(20)]
    assert [c.start for c in ot_c] == [T0 + timedelta(milliseconds=50 * i) for i in range(15)]
    # Interleaved in time order, not one file after the other.
    assert [c.channel for c in chunks[:4]] == [Channel.ME, Channel.OTHERS] * 2
    assert all(b.start >= a.start for a, b in pairwise(chunks))
    assert all(len(c.pcm_f32le) == 4 * 800 for c in chunks)
    assert np.allclose(np.frombuffer(me_c[5].pcm_f32le, "<f4"), 0.25, atol=1e-3)
    assert np.allclose(np.frombuffer(ot_c[5].pcm_f32le, "<f4"), -0.5, atol=1e-3)


async def test_single_file_and_short_tail(tmp_path: Path) -> None:
    others = _write_wav(tmp_path / "o.wav", np.zeros(16_000 + 160), 16_000)
    chunks = await _collect(FileAudioSource(None, others, start=T0, chunk_ms=100))
    assert {c.channel for c in chunks} == {Channel.OTHERS}
    assert len(chunks) == 11
    assert len(chunks[-1].pcm_f32le) == 4 * 160


@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_decodes_pcm_widths(tmp_path: Path, width: int) -> None:
    x = np.array([0.0, 0.5, -0.5, 0.25], dtype=np.float32)
    rate, blocks = read_wav_blocks(_write_wav(tmp_path / "x.wav", x, 8000, width))
    y = np.concatenate(list(blocks))
    assert rate == 8000
    assert y.shape == (4, 1)
    np.testing.assert_allclose(y[:, 0], x, atol=1 / 64)


def test_needs_a_file_and_lists_devices(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        FileAudioSource(None, None)
    me = tmp_path / "me.wav"
    devs = FileAudioSource(me, None).list_devices()
    assert [(d.name, d.is_input) for d in devs] == [("me.wav", True)]


async def test_realtime_paces_chunks(tmp_path: Path) -> None:
    me = _write_wav(tmp_path / "me.wav", np.zeros(3_200), 16_000)  # 0.2 s
    t = time.monotonic()
    chunks = await _collect(FileAudioSource(me, None, realtime=True, chunk_ms=50))
    assert len(chunks) == 4
    assert time.monotonic() - t >= 0.18
    assert chunks[0].start.tzinfo is not None
    assert SAMPLE_RATE == 16_000


async def test_close_stops_the_stream(tmp_path: Path) -> None:
    me = _write_wav(tmp_path / "me.wav", np.zeros(16_000), 16_000)
    src = FileAudioSource(me, None, start=T0)
    got = []
    async for c in src.stream():
        got.append(c)
        await src.close()
    assert len(got) == 1
