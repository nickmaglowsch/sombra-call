"""Voice-activity segmentation: audio frames in, closed utterances out (T1).

``Segmenter`` is the pure-logic core: it slices a channel's PCM into 32 ms frames, asks a
``SpeechDetector`` (Silero VAD in production, a fake in tests) for a speech probability per
frame, and closes an utterance after ``min_silence_ms`` of silence or, when an utterance
runs past ``max_utterance_s``, forces a cut at the quietest frame of its last few seconds.

Two energy gates keep silence cheap: frames under ``energy_gate_dbfs`` (digital silence,
a muted mic), and frames within ``noise_margin_db`` of the channel's tracked noise floor
(steady room tone or fan noise), are scored 0 without calling the detector. Silence then
costs a few numpy ops per frame and never produces a segment.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import numpy as np
import numpy.typing as npt

from sombra.contracts import SAMPLE_RATE

FRAME_SAMPLES = 512  # what Silero VAD takes at 16 kHz: 32 ms
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE

Pcm = npt.NDArray[np.float32]


class SpeechDetector(Protocol):
    """Speech probability for one ``FRAME_SAMPLES`` frame. Stateful; one per channel."""

    def __call__(self, frame: Pcm) -> float: ...

    def reset(self) -> None: ...


@dataclass(frozen=True, slots=True)
class VadSettings:
    threshold: float = 0.5  # a frame at or above this starts / continues speech
    neg_threshold: float | None = None  # below this counts as silence; default threshold - 0.15
    min_silence_ms: int = 500  # silence that closes an utterance
    min_speech_ms: int = 250  # shorter utterances are dropped (clicks, coughs)
    speech_pad_ms: int = 160  # audio kept before the first and after the last speech frame
    max_utterance_s: float = 15.0  # longer utterances are cut at a pause
    cut_search_s: float = 3.0  # how far back from the limit to look for that pause
    energy_gate_dbfs: float | None = -60.0  # frames quieter than this skip the detector
    noise_margin_db: float | None = 10.0  # ...and so do frames this close to the noise floor
    noise_floor_rise_db: float = 0.01  # per frame (0.3 dB/s): the floor falls fast, rises slowly

    def __post_init__(self) -> None:
        if not 0.0 < self.threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        if self.neg_threshold is not None and not 0.0 <= self.neg_threshold <= self.threshold:
            raise ValueError("neg_threshold must be in [0, threshold]")
        if self.max_utterance_s <= self.cut_search_s:
            raise ValueError("max_utterance_s must be longer than cut_search_s")

    @property
    def silence_below(self) -> float:
        if self.neg_threshold is not None:
            return self.neg_threshold
        return max(0.0, self.threshold - 0.15)


@dataclass(frozen=True, slots=True)
class Segment:
    """One closed utterance of one channel."""

    start: datetime  # first speech frame (not the pre-roll pad)
    end: datetime  # end of the last speech frame, or the cut point on a forced cut
    audio: Pcm  # includes the pads; what goes to whisper
    forced_cut: bool = False

    @property
    def duration_s(self) -> float:
        return len(self.audio) / SAMPLE_RATE


_FLOOR_MIN_DB = -100.0


def _frames(ms: float) -> int:
    return max(1, math.ceil(ms / 1000 / FRAME_SECONDS))


class Segmenter:
    """Streaming VAD state machine for one channel. Not thread-safe; feed it from one thread."""

    def __init__(self, detector: SpeechDetector, settings: VadSettings | None = None) -> None:
        self._detector = detector
        self._s = settings or VadSettings()
        self._min_silence = _frames(self._s.min_silence_ms)
        self._min_speech = _frames(self._s.min_speech_ms)
        self._pad = _frames(self._s.speech_pad_ms) if self._s.speech_pad_ms > 0 else 0
        self._max = _frames(self._s.max_utterance_s * 1000)
        self._search = _frames(self._s.cut_search_s * 1000)
        gate = self._s.energy_gate_dbfs
        self._gate_db = -math.inf if gate is None else gate
        # starts at the absolute gate, not at the first frame, so a stream that opens
        # mid-sentence is not taken for noise; steady noise pulls it up within seconds
        self._floor_db = max(self._gate_db, _FLOOR_MIN_DB)

        self._rest: Pcm = np.zeros(0, dtype=np.float32)  # < 1 frame carried between feeds
        self._rest_t0: datetime | None = None
        self._preroll: deque[tuple[datetime, Pcm]] = deque(maxlen=max(1, self._pad))
        # current utterance: parallel lists of frame start times, audio, probabilities
        self._times: list[datetime] = []
        self._audio: list[Pcm] = []
        self._probs: list[float] = []
        self._silence_from: int | None = None  # first silent frame after the last speech frame
        self.detector_calls = 0

    @property
    def speaking(self) -> bool:
        return bool(self._times)

    def feed(self, start: datetime, pcm: Pcm) -> list[Segment]:
        """Add samples that begin at ``start``; return utterances this closed."""
        if len(self._rest) == 0:
            self._rest_t0 = start
            buf = pcm
        else:
            buf = np.concatenate([self._rest, pcm])
        t0 = self._rest_t0 if self._rest_t0 is not None else start
        closed: list[Segment] = []
        n = len(buf) // FRAME_SAMPLES
        for i in range(n):
            frame = buf[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]
            t = t0 + timedelta(seconds=i * FRAME_SECONDS)
            closed.extend(self._step(t, frame))
        self._rest = buf[n * FRAME_SAMPLES :]
        self._rest_t0 = t0 + timedelta(seconds=n * FRAME_SECONDS)
        return closed

    def flush(self) -> list[Segment]:
        """End of stream: close the open utterance, if any, and reset."""
        out = self._close(len(self._probs)) if self.speaking else []
        self._clear()
        self._preroll.clear()
        self._rest = np.zeros(0, dtype=np.float32)
        self._rest_t0 = None
        self._detector.reset()
        return out

    # --- internals ---------------------------------------------------------------------

    def _score(self, frame: Pcm) -> float:
        if self._gated(frame):
            return 0.0
        self.detector_calls += 1
        return float(self._detector(frame))

    def _gated(self, frame: Pcm) -> bool:
        margin = self._s.noise_margin_db
        if self._gate_db == -math.inf and margin is None:
            return False
        power = float(np.mean(np.square(frame, dtype=np.float64)))
        db = max(_FLOOR_MIN_DB, 10 * math.log10(power)) if power > 0 else _FLOOR_MIN_DB
        if margin is None:
            return db < self._gate_db
        if db < self._floor_db:
            self._floor_db = db
        else:
            self._floor_db = min(db, self._floor_db + self._s.noise_floor_rise_db)
        return db < self._gate_db or db < self._floor_db + margin

    def _step(self, t: datetime, frame: Pcm) -> list[Segment]:
        p = self._score(frame)
        if not self.speaking:
            if p >= self._s.threshold:
                for pt, pa in self._preroll:
                    self._push(pt, pa, 0.0)
                self._preroll.clear()
                self._push(t, frame, p)
            elif self._pad:
                self._preroll.append((t, frame))
            return []

        self._push(t, frame, p)
        n = len(self._probs)
        if self._silence_from is not None and n - self._silence_from >= self._min_silence:
            out = self._close(n)
            self._restart_from(n - self._pad)  # trailing silence becomes the next pre-roll
            return out
        if len(self._probs) >= self._max:
            return self._forced_cut()
        return []

    def _push(self, t: datetime, frame: Pcm, p: float) -> None:
        if p >= self._s.threshold:
            self._silence_from = None
        elif p < self._s.silence_below and self._silence_from is None:
            self._silence_from = len(self._probs)
        self._times.append(t)
        self._audio.append(frame)
        self._probs.append(p)

    def _speech_idx(self) -> list[int]:
        return [i for i, p in enumerate(self._probs) if p >= self._s.threshold]

    def _close(self, upto: int, *, forced: bool = False) -> list[Segment]:
        """Emit frames ``[:upto]`` as a segment if they hold enough speech."""
        speech = [i for i in self._speech_idx() if i < upto]
        if len(speech) < self._min_speech:
            return []
        first, last = speech[0], speech[-1]
        stop = upto if forced else min(upto, last + 1 + self._pad)
        begin = max(0, first - self._pad)
        end_idx = upto if forced else last + 1
        end = self._times[end_idx - 1] + timedelta(seconds=FRAME_SECONDS)
        audio = np.concatenate(self._audio[begin:stop]).astype(np.float32, copy=False)
        return [Segment(start=self._times[first], end=end, audio=audio, forced_cut=forced)]

    def _forced_cut(self) -> list[Segment]:
        n = len(self._probs)
        lo = max(1, n - self._search)
        window = self._probs[lo:n]
        # quietest frame; on ties take the latest so the cut keeps as much as possible
        cut = lo + max(range(len(window)), key=lambda i: (-window[i], i))
        out = self._close(cut, forced=True)
        self._restart_from(cut)
        return out

    def _restart_from(self, idx: int) -> None:
        """Keep frames ``[idx:]`` as the start of a new utterance (or pre-roll if silent)."""
        times, audio, probs = self._times[idx:], self._audio[idx:], self._probs[idx:]
        self._clear()
        speech = [i for i, p in enumerate(probs) if p >= self._s.threshold]
        if not speech:
            if self._pad:
                self._preroll.extend(zip(times[-self._pad :], audio[-self._pad :], strict=True))
            return
        for t, a, p in zip(times, audio, probs, strict=True):
            self._push(t, a, p)

    def _clear(self) -> None:
        self._times, self._audio, self._probs = [], [], []
        self._silence_from = None


def pcm_from_bytes(pcm_f32le: bytes) -> Pcm:
    """Decode an ``AudioChunk`` payload (mono float32 little-endian)."""
    return np.frombuffer(pcm_f32le, dtype="<f4").astype(np.float32, copy=False)
