"""Pure signal processing: downmix, streaming resample to 16 kHz, fixed-size chunking.

No platform code here, so everything is unit-tested on every OS. The capture callbacks
call :class:`Chunker` directly, so it must stay cheap and allocation-light.
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

from sombra.contracts import SAMPLE_RATE

Samples = NDArray[np.float32]

MIN_CHUNK_MS = 20
MAX_CHUNK_MS = 100


def to_mono(frames: NDArray[np.floating] | NDArray[np.integer]) -> Samples:
    """Average ``(n,)`` or ``(n, channels)`` frames into mono float32."""
    x = np.asarray(frames, dtype=np.float32)
    if x.ndim == 1:
        return x
    if x.ndim != 2:
        raise ValueError(f"expected (n,) or (n, channels) frames, got shape {x.shape}")
    if x.shape[1] == 1:
        return np.ascontiguousarray(x[:, 0])
    return x.mean(axis=1, dtype=np.float32)


def _lowpass_taps(in_rate: int, out_rate: int) -> Samples:
    """Kaiser-windowed sinc anti-aliasing filter for downsampling ``in_rate`` -> ``out_rate``."""
    ratio = in_rate / out_rate
    half = 8 * math.ceil(ratio)  # taps per side; 49 taps for 48 kHz -> 16 kHz
    n = np.arange(-half, half + 1, dtype=np.float64)
    cutoff = 0.45 / ratio  # cycles/sample at in_rate; a bit below the output Nyquist
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.kaiser(2 * half + 1, 8.0)
    return (h / h.sum()).astype(np.float32)


class StreamingResampler:
    """Band-limited resampler for a stream fed in arbitrary block sizes.

    Output sample ``k`` is the input signal at time ``k / out_rate`` exactly (the
    filter delay is compensated in the read position, not in timestamps), so a chunk
    starting at output index ``k`` can be stamped with the time of input sample
    ``k * in_rate / out_rate``. Feeding the same signal in any block sizes gives the
    same output. Upsampling works too (linear interpolation, no filter).
    """

    def __init__(self, in_rate: int, out_rate: int = SAMPLE_RATE) -> None:
        if in_rate <= 0 or out_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.in_rate = in_rate
        self.out_rate = out_rate
        self._taps = _lowpass_taps(in_rate, out_rate) if in_rate > out_rate else None
        ntaps = 1 if self._taps is None else len(self._taps)
        self._delay = (ntaps - 1) // 2
        self._hist = np.zeros(ntaps - 1, dtype=np.float32)  # filter history
        self._y = np.zeros(0, dtype=np.float32)  # filtered samples not yet consumed
        self._y_base = 0  # absolute index of self._y[0] in the filtered stream
        self._k = 0  # next output sample index
        self._n_in = 0

    @property
    def samples_in(self) -> int:
        return self._n_in

    @property
    def samples_out(self) -> int:
        return self._k

    def process(self, x: Samples) -> Samples:
        """Feed mono samples; return every output sample that can be computed so far."""
        if self.in_rate == self.out_rate:
            self._n_in += len(x)
            self._k += len(x)
            return np.asarray(x, dtype=np.float32)
        self._n_in += len(x)
        if self._taps is not None:
            buf = np.concatenate((self._hist, x))
            filtered = np.convolve(buf, self._taps, mode="valid").astype(np.float32)
            self._hist = buf[len(buf) - len(self._hist) :]
        else:
            filtered = np.asarray(x, dtype=np.float32)
        self._y = np.concatenate((self._y, filtered))
        y_end = self._y_base + len(self._y)  # exclusive
        # Position of output k in the filtered stream: delay + k * in / out.
        # Output k needs filtered samples floor(pos) and floor(pos) + 1.
        # Largest k with floor(pos_k) + 1 < y_end:
        #   delay + (k * in) // out <= y_end - 2  <=>  k * in < (y_end - 1 - delay) * out
        limit = (y_end - 1 - self._delay) * self.out_rate
        k_end = max(self._k, -(-limit // self.in_rate))  # ceil division
        if k_end == self._k:
            return np.zeros(0, dtype=np.float32)
        k = np.arange(self._k, k_end, dtype=np.int64)
        num = k * self.in_rate
        idx = self._delay + num // self.out_rate - self._y_base
        frac = ((num % self.out_rate) / self.out_rate).astype(np.float32)
        out = self._y[idx] * (1 - frac) + self._y[idx + 1] * frac
        self._k = k_end
        # Drop filtered samples no future output needs.
        keep_from = self._delay + (self._k * self.in_rate) // self.out_rate - self._y_base
        keep_from = min(keep_from, len(self._y))  # the next output may need unseen input
        if keep_from > 0:
            self._y = self._y[keep_from:]
            self._y_base += keep_from
        return out.astype(np.float32, copy=False)

    def flush(self) -> Samples:
        """End of stream: return the remaining samples, up to ``ceil(n_in * out / in)`` total."""
        total = -(-self._n_in * self.out_rate // self.in_rate)
        pad = np.zeros(self._delay + 2 + math.ceil(self.in_rate / self.out_rate), np.float32)
        n_in = self._n_in
        out = self.process(pad)
        self._n_in = n_in
        keep = max(0, total - (self._k - len(out)))
        self._k -= len(out) - min(keep, len(out))
        return out[:keep]


class Chunker:
    """Mono-mix, resample to 16 kHz and cut into fixed ``chunk_ms`` chunks.

    :meth:`push` returns ``(first_sample_index, samples)`` pairs; the index counts
    16 kHz output samples since the stream started, which the caller turns into a
    timestamp.
    """

    def __init__(self, in_rate: int, chunk_ms: int = 50) -> None:
        if not MIN_CHUNK_MS <= chunk_ms <= MAX_CHUNK_MS:
            raise ValueError(f"chunk_ms must be in [{MIN_CHUNK_MS}, {MAX_CHUNK_MS}]")
        self.chunk_samples = SAMPLE_RATE * chunk_ms // 1000
        self.resampler = StreamingResampler(in_rate, SAMPLE_RATE)
        self._pending = np.zeros(0, dtype=np.float32)
        self._next_index = 0  # output index of self._pending[0]

    def push(self, frames: NDArray[np.floating] | NDArray[np.integer]) -> list[tuple[int, Samples]]:
        return self._cut(self.resampler.process(to_mono(frames)), final=False)

    def flush(self) -> list[tuple[int, Samples]]:
        """End of stream: emit what is left, the last chunk possibly shorter."""
        return self._cut(self.resampler.flush(), final=True)

    def _cut(self, y: Samples, *, final: bool) -> list[tuple[int, Samples]]:
        buf = np.concatenate((self._pending, y)) if len(self._pending) else y
        n = self.chunk_samples
        full = len(buf) // n
        out = [(self._next_index + i * n, buf[i * n : (i + 1) * n]) for i in range(full)]
        rest = buf[full * n :]
        self._next_index += full * n
        if final and len(rest):
            out.append((self._next_index, rest))
            self._next_index += len(rest)
            rest = rest[:0]
        self._pending = np.array(rest, dtype=np.float32)
        return out
