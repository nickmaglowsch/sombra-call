import asyncio
from itertools import pairwise

import numpy as np
import pytest
from audio_fakes import FakeSounddevice, FakeTap

from sombra.audio.devices import TAP_ID
from sombra.audio.macos import MacAudioSource
from sombra.contracts import SAMPLE_RATE, AudioSource, Channel


def _source(sd: FakeSounddevice, **kw: object) -> tuple[MacAudioSource, list[FakeTap]]:
    taps: list[FakeTap] = []

    def factory() -> FakeTap:
        taps.append(FakeTap(sd))
        return taps[-1]

    kw.setdefault("macos_release", "14.5")
    src = MacAudioSource(sounddevice=sd, tap_factory=factory, **kw)  # type: ignore[arg-type]
    return src, taps


def test_is_an_audio_source() -> None:
    src: AudioSource = _source(FakeSounddevice())[0]
    assert src.list_devices()[-1].id == "BlackHole 2ch"


def test_auto_uses_process_tap_on_14_4() -> None:
    sd = FakeSounddevice()
    src, taps = _source(sd)
    src.start()
    assert src.system_path == "process-tap"
    assert sd.reinitialised == 1
    mic, system = sd.streams
    assert mic.kwargs["device"] == 0  # default input
    assert system.kwargs["device"] == 5  # the aggregate device PortAudio found
    assert (system.kwargs["samplerate"], system.kwargs["channels"]) == (48_000, 2)
    assert mic.started and system.started
    assert src.mic_id == "MacBook Pro Microphone"
    src.start()  # idempotent
    assert len(sd.streams) == 2
    assert not taps[0].destroyed


def test_auto_falls_back_to_blackhole_before_14_4() -> None:
    sd = FakeSounddevice()
    src, taps = _source(sd, macos_release="14.2")
    src.start()
    assert taps == []
    assert src.system_path == "loopback:BlackHole 2ch"
    assert sd.streams[1].kwargs["device"] == 2


def test_auto_falls_back_to_blackhole_when_tap_fails() -> None:
    sd = FakeSounddevice()

    def broken() -> FakeTap:
        raise OSError("permission denied")

    src = MacAudioSource(sounddevice=sd, tap_factory=broken, macos_release="15.0")
    src.start()
    assert src.system_path == "loopback:BlackHole 2ch"


def test_explicit_tap_does_not_fall_back() -> None:
    sd = FakeSounddevice()

    def broken() -> FakeTap:
        raise OSError("permission denied")

    src = MacAudioSource(sounddevice=sd, system=TAP_ID, tap_factory=broken, macos_release="15")
    with pytest.raises(OSError, match="permission"):
        src.start()


def test_tap_is_destroyed_if_portaudio_cannot_see_it() -> None:
    sd = FakeSounddevice()
    tap = FakeTap(sd, name="other")
    sd.after_reinit = []  # PortAudio never lists the aggregate
    src = MacAudioSource(sounddevice=sd, system=TAP_ID, tap_factory=lambda: tap, macos_release="15")
    with pytest.raises(LookupError):
        src.start()
    assert tap.destroyed


def test_no_system_source_is_a_clear_error() -> None:
    sd = FakeSounddevice([d for d in FakeSounddevice().devices if "BlackHole" not in d["name"]])
    src, _ = _source(sd, macos_release="13.6")
    with pytest.raises(LookupError, match="BlackHole"):
        src.start()


def test_explicit_devices_by_id() -> None:
    sd = FakeSounddevice()
    src, _ = _source(sd, mic="USB Mic#2", system="BlackHole 2ch")
    src.start()
    mic, system = sd.streams
    assert (mic.kwargs["device"], mic.kwargs["samplerate"], mic.kwargs["channels"]) == (
        4,
        16_000,
        1,
    )
    assert src.system_path == "loopback:BlackHole 2ch"
    assert system.kwargs["device"] == 2


def test_failed_open_cleans_up() -> None:
    sd = FakeSounddevice()
    sd.fail_start_on = 5
    src, taps = _source(sd)
    with pytest.raises(OSError, match="busy"):
        src.start()
    assert sd.streams[0].closed
    assert taps[0].destroyed


async def test_stream_yields_both_channels_and_close_releases_everything() -> None:
    sd = FakeSounddevice()
    src, taps = _source(sd, mic="USB Mic", chunk_ms=20)
    gen = src.stream()
    task = asyncio.ensure_future(gen.__anext__())  # starts capture
    await asyncio.sleep(0)
    mic, system = sd.streams
    mic.feed(0.2, value=0.25)
    system.feed(0.2, value=-0.5)
    chunks = [await task]
    while len(chunks) < 18:
        chunks.append(await gen.__anext__())
    await gen.aclose()
    by_channel = {ch: [c for c in chunks if c.channel is ch] for ch in Channel}
    assert len(by_channel[Channel.ME]) >= 8 and len(by_channel[Channel.OTHERS]) >= 8
    for ch, level in ((Channel.ME, 0.25), (Channel.OTHERS, -0.5)):
        cs = by_channel[ch]
        assert all(len(c.pcm_f32le) == 4 * SAMPLE_RATE // 50 for c in cs)
        assert all(b.start > a.start for a, b in pairwise(cs))
        assert np.allclose(np.frombuffer(cs[-1].pcm_f32le, "<f4"), level, atol=1e-3)
    assert src.overflows == 2
    assert all(s.stopped and s.closed for s in sd.streams)
    assert taps[0].destroyed
    await src.close()  # idempotent


async def test_stalled_consumer_drops_oldest() -> None:
    sd = FakeSounddevice()
    src, _ = _source(sd, chunk_ms=50, max_buffer_s=0.5)  # 20 chunks total
    src.start()
    sd.streams[0].feed(2.0)
    # ~2 s = 39-40 chunks (the resampler holds back a few ms); only the newest 20 stay.
    assert len(src._queue) == 20
    assert src.dropped_chunks >= 19
    await src.close()


def test_callback_errors_are_contained(caplog: pytest.LogCaptureFixture) -> None:
    sd = FakeSounddevice()
    src, _ = _source(sd)
    src.start()
    sd.streams[0].callback(np.zeros((10, 2, 2), np.float32), 10, None, None)
    assert "audio callback failed" in caplog.text


def test_default_sounddevice_import_is_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    import sombra.audio.macos as macos

    sd = FakeSounddevice()
    monkeypatch.setattr(macos, "_load_sounddevice", lambda: sd)
    src = MacAudioSource(macos_release="14.0")
    assert [d.id for d in src.list_devices()][-1] == "BlackHole 2ch"


@pytest.mark.parametrize("dev", [3, None, (None, 1)])
def test_default_device_shapes(dev: object) -> None:
    sd = FakeSounddevice()
    sd.default.device = dev
    src, _ = _source(sd, macos_release="14.0")
    src.start()
    assert sd.streams[0].kwargs["device"] == (3 if dev == 3 else 0)


def test_tap_module_imports_everywhere() -> None:
    from sombra.audio._coreaudio_tap import TAP_DEVICE_PREFIX, CoreAudioTap

    tap = CoreAudioTap(tap_id=1, aggregate_id=2, device_name=f"{TAP_DEVICE_PREFIX} x")
    assert tap.device_name.startswith("Sombra")
