import numpy as np
import pytest

from sombra.audio.dsp import Chunker, StreamingResampler, to_mono
from sombra.contracts import SAMPLE_RATE


def _sine(freq: float, rate: int, seconds: float, phase: float = 0.0) -> np.ndarray:
    t = np.arange(round(rate * seconds)) / rate
    return np.sin(2 * np.pi * freq * t + phase).astype(np.float32)


def _feed(r: StreamingResampler, x: np.ndarray, sizes: list[int]) -> np.ndarray:
    out, i = [], 0
    while i < len(x):
        n = sizes[len(out) % len(sizes)]
        out.append(r.process(x[i : i + n]))
        i += n
    out.append(r.flush())
    return np.concatenate(out)


def test_to_mono_averages_channels() -> None:
    stereo = np.array([[1.0, 0.0], [0.5, 0.5], [-1.0, 1.0]], dtype=np.float32)
    assert to_mono(stereo).tolist() == [0.5, 0.5, 0.0]
    assert to_mono(stereo[:, :1]).tolist() == [1.0, 0.5, -1.0]
    assert to_mono(np.array([0.25, 0.5])).dtype == np.float32
    with pytest.raises(ValueError, match="shape"):
        to_mono(np.zeros((2, 2, 2)))


@pytest.mark.parametrize("in_rate", [48_000, 44_100, 96_000])
def test_resample_stereo_to_16k_mono_matches_ideal(in_rate: int) -> None:
    # Left and right differ in amplitude: mono is their mean.
    left = 0.8 * _sine(440.0, in_rate, 1.0)
    right = 0.4 * _sine(440.0, in_rate, 1.0)
    stereo = np.stack([left, right], axis=1)
    r = StreamingResampler(in_rate)
    y = _feed(r, to_mono(stereo), [480, 512, 1000, 37])
    assert len(y) == SAMPLE_RATE  # exactly 1 s of output
    expected = 0.6 * _sine(440.0, SAMPLE_RATE, 1.0)
    # Ignore the filter's start/end transients (zero-padded edges).
    core = slice(200, -200)
    assert np.max(np.abs(y[core] - expected[core])) < 0.01
    assert y.dtype == np.float32


def test_resample_is_independent_of_block_sizes() -> None:
    x = np.random.default_rng(1).standard_normal(48_000).astype(np.float32)
    a = _feed(StreamingResampler(48_000), x, [48_000])
    b = _feed(StreamingResampler(48_000), x, [1, 7, 480, 333, 4096])
    assert len(a) == len(b) == 16_000
    np.testing.assert_allclose(a, b, atol=1e-5)


def test_resample_rejects_aliases_above_output_nyquist() -> None:
    x = _sine(11_000.0, 48_000, 1.0)  # would alias to 5 kHz at 16 kHz without a filter
    y = _feed(StreamingResampler(48_000), x, [480])
    rms = float(np.sqrt(np.mean(y[200:-200] ** 2)))
    assert rms < 0.01  # > 37 dB down from the 0.707 RMS input


def test_passthrough_and_upsampling() -> None:
    x = _sine(300.0, 16_000, 0.1)
    same = StreamingResampler(16_000)
    np.testing.assert_array_equal(_feed(same, x, [160]), x)
    assert same.samples_in == same.samples_out == len(x)

    up = StreamingResampler(8_000)
    y = _feed(up, _sine(300.0, 8_000, 0.5), [80])
    assert len(y) == 8_000
    expected = _sine(300.0, 16_000, 0.5)
    assert np.max(np.abs(y[10:-10] - expected[10:-10])) < 0.02


def test_invalid_rates() -> None:
    with pytest.raises(ValueError):
        StreamingResampler(0)


def test_chunker_cuts_fixed_chunks_with_sample_indexes() -> None:
    c = Chunker(48_000, chunk_ms=20)
    assert c.chunk_samples == 320
    out = []
    x = np.zeros((48_000, 2), dtype=np.float32)  # 1 s stereo
    for i in range(0, len(x), 441):
        out += c.push(x[i : i + 441])
    out += c.flush()
    indexes = [i for i, _ in out]
    assert indexes == list(range(0, 16_000, 320))
    assert all(len(s) == 320 for _, s in out)


def test_chunker_flush_emits_short_tail() -> None:
    c = Chunker(16_000, chunk_ms=100)
    assert c.push(np.zeros(2_000, dtype=np.float32)) == [(0, pytest.approx(np.zeros(1600)))]
    tail = c.flush()
    assert [(i, len(s)) for i, s in tail] == [(1600, 400)]
    assert c.flush() == []


@pytest.mark.parametrize("ms", [19, 101])
def test_chunk_ms_bounds(ms: int) -> None:
    with pytest.raises(ValueError, match="chunk_ms"):
        Chunker(48_000, chunk_ms=ms)
