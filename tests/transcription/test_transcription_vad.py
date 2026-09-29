"""VAD segmentation rules: min silence closes, max length cuts at a pause, gating, timing."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest
from transcription_fakes import T0, AmplitudeDetector, concat, silence, tone

from sombra.transcription.vad import (
    FRAME_SECONDS,
    Segment,
    Segmenter,
    VadSettings,
    pcm_from_bytes,
)

FRAME = timedelta(seconds=FRAME_SECONDS)


def run(
    pcm: np.ndarray, *, chunk: int = 1600, settings: VadSettings | None = None
) -> list[Segment]:
    seg = Segmenter(AmplitudeDetector(), settings)
    out: list[Segment] = []
    for i in range(0, len(pcm), chunk):
        out += seg.feed(T0 + timedelta(seconds=i / 16000), pcm[i : i + chunk])
    return out + seg.flush()


def near(actual: object, seconds: float, tol: timedelta = FRAME) -> bool:
    assert isinstance(actual, type(T0))
    return abs(actual - (T0 + timedelta(seconds=seconds))) <= tol


def test_one_utterance_has_speech_start_and_end() -> None:
    [s] = run(concat(silence(1.0), tone(2.0), silence(1.0)))
    assert near(s.start, 1.0)
    assert near(s.end, 3.0)
    assert not s.forced_cut
    # 160 ms of padding on each side goes to whisper, not more
    assert 2.0 <= s.duration_s <= 2.0 + 2 * 0.16 + 2 * FRAME_SECONDS


def test_utterance_closes_only_after_min_silence() -> None:
    seg = Segmenter(AmplitudeDetector())
    assert seg.feed(T0, concat(tone(2.0), silence(0.4))) == []
    assert seg.speaking
    [s] = seg.feed(T0 + timedelta(seconds=2.4), silence(0.2))
    assert near(s.end, 2.0)
    assert not seg.speaking


def test_pause_shorter_than_min_silence_does_not_split() -> None:
    [s] = run(concat(tone(1.0), silence(0.3), tone(1.0), silence(1.0)))
    assert near(s.start, 0.0)
    assert near(s.end, 2.3)


def test_min_silence_is_configurable() -> None:
    pcm = concat(tone(1.0), silence(0.3), tone(1.0), silence(1.0))
    assert len(run(pcm, settings=VadSettings(min_silence_ms=200))) == 2


def test_short_blip_is_dropped() -> None:
    assert run(concat(silence(0.5), tone(0.1), silence(1.0))) == []


def test_two_utterances_in_order() -> None:
    a, b = run(concat(tone(1.0), silence(1.0), tone(1.5), silence(1.0)))
    assert near(a.start, 0.0) and near(b.start, 2.0)
    assert a.end < b.start


def test_max_length_cuts_at_the_pause() -> None:
    # 13 s, a 200 ms breath (too short to close), 5 s more: the cut lands in the breath
    pcm = concat(tone(13.0), silence(0.2), tone(5.0), silence(1.0))
    first, second = run(pcm)
    assert first.forced_cut
    assert T0 + timedelta(seconds=13.0) - FRAME <= first.end <= T0 + timedelta(seconds=13.2) + FRAME
    assert first.duration_s <= 15.0
    assert near(second.start, 13.2, tol=2 * FRAME)
    assert near(second.end, 18.2)


def test_max_length_without_pause_cuts_hard_at_the_limit() -> None:
    first, second = run(concat(tone(20.0), silence(1.0)))
    assert first.forced_cut
    assert 14.5 <= first.duration_s <= 15.0
    assert near(second.end, 20.0)
    assert second.start == first.end


def test_max_utterance_is_configurable() -> None:
    segs = run(concat(tone(9.0), silence(1.0)), settings=VadSettings(max_utterance_s=4.0))
    assert len(segs) == 3
    assert all(s.duration_s <= 4.0 + 0.16 for s in segs)


def test_chunk_size_does_not_change_segments() -> None:
    pcm = concat(silence(0.7), tone(1.2), silence(0.8), tone(0.9), silence(1.0))
    small = [(s.start, s.end, len(s.audio)) for s in run(pcm, chunk=100)]
    big = [(s.start, s.end, len(s.audio)) for s in run(pcm, chunk=16000)]
    assert small == big and len(small) == 2


def test_segment_time_follows_chunk_timestamps_across_a_gap() -> None:
    seg = Segmenter(AmplitudeDetector())
    assert seg.feed(T0, silence(1.024)) == []  # exactly 32 frames: nothing carried over
    later = T0 + timedelta(minutes=5)
    [s] = seg.feed(later, concat(tone(1.0), silence(1.0)))
    assert abs(s.start - later) <= FRAME


def test_silence_never_reaches_the_detector() -> None:
    det = AmplitudeDetector()
    seg = Segmenter(det)
    for i in range(600):  # one minute
        assert seg.feed(T0 + timedelta(seconds=i / 10), silence(0.1, seed=i)) == []
    assert det.calls == 0 and seg.detector_calls == 0
    assert seg.flush() == []


def test_without_energy_gate_detector_sees_every_frame() -> None:
    det = AmplitudeDetector()
    seg = Segmenter(det, VadSettings(energy_gate_dbfs=None, noise_margin_db=None))
    seg.feed(T0, silence(1.024))
    assert det.calls == 32


def test_steady_room_noise_is_gated_after_the_floor_adapts() -> None:
    """-50 dBFS room tone is above the absolute gate; the noise floor learns it."""
    det = AmplitudeDetector(level=1.0)  # never speech
    seg = Segmenter(det)
    noise = silence(60.0, level=3e-3, seed=3)
    seg.feed(T0, noise[: 16000 * 10])
    early = det.calls
    seg.feed(T0 + timedelta(seconds=10), noise[16000 * 10 :])
    assert early < 50 * 16000 * 10 // 512 // 100  # < 50% of the first 10 s reached the model
    assert det.calls - early == 0  # after that, none


def test_speech_right_at_stream_start_is_not_taken_for_noise() -> None:
    [s] = run(concat(tone(2.0), silence(1.0)))
    assert near(s.start, 0.0)


def test_speech_over_room_noise_still_detected() -> None:
    noise = silence(40.0, level=3e-3, seed=5)
    speech = tone(1.5, amp=0.1) + silence(1.5, level=3e-3, seed=6)
    [s] = run(concat(noise, speech, silence(1.0, level=3e-3, seed=8)))
    assert near(s.start, 40.0)


def test_flush_closes_open_utterance_and_resets_detector() -> None:
    det = AmplitudeDetector()
    seg = Segmenter(det)
    assert seg.feed(T0, tone(2.0)) == []
    [s] = seg.flush()
    assert near(s.end, 2.0)
    assert det.resets == 1
    assert seg.flush() == []


def test_pre_roll_keeps_audio_before_speech() -> None:
    [s] = run(concat(silence(1.0), tone(1.0), silence(1.0)))
    assert float(np.abs(s.audio[:1000]).max()) < 0.01  # starts with the quiet pad
    assert s.duration_s >= 1.0 + 0.15


def test_settings_validation() -> None:
    with pytest.raises(ValueError, match="threshold"):
        VadSettings(threshold=0)
    with pytest.raises(ValueError, match="neg_threshold"):
        VadSettings(threshold=0.5, neg_threshold=0.6)
    with pytest.raises(ValueError, match="max_utterance_s"):
        VadSettings(max_utterance_s=2.0, cut_search_s=3.0)
    assert VadSettings(threshold=0.5).silence_below == pytest.approx(0.35)
    assert VadSettings(neg_threshold=0.2).silence_below == 0.2


def test_pcm_from_bytes_round_trip() -> None:
    pcm = tone(0.01)
    assert np.array_equal(pcm_from_bytes(pcm.astype("<f4").tobytes()), pcm)
