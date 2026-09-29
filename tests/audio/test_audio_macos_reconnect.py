"""MacAudioSource reconnect (A3) against a hot-plug fake of PortAudio + Core Audio.

A pump thread plays PortAudio's device threads: it feeds every open stream whose device
is still plugged in. Timeouts are shortened so each scenario runs well under a second.
"""

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from itertools import pairwise
from typing import Any

import pytest
from audio_fakes import FakeTap, FakeWatcher, HotplugSounddevice

from sombra.audio.macos import MacAudioSource
from sombra.audio.reconnect import Backoff, SourceStatus
from sombra.contracts import Channel

ME, OTHERS = Channel.ME, Channel.OTHERS
BUILTIN = "MacBook Pro Microphone"


class Rig:
    def __init__(self, *, watcher: bool = True, **kw: Any) -> None:
        self.sd = HotplugSounddevice()
        self.watcher = FakeWatcher(self.sd, default_input=kw.pop("default_input", None))
        self.taps: list[FakeTap] = []
        self.events: list[SourceStatus] = []
        self._lock = threading.Lock()

        def tap_factory() -> FakeTap:
            self.taps.append(FakeTap(self.sd, name=f"Sombra system audio {len(self.taps)}"))
            return self.taps[-1]

        kw.setdefault("macos_release", "14.5")
        self.src = MacAudioSource(
            sounddevice=self.sd,
            tap_factory=tap_factory,
            watcher_factory=(lambda: self.watcher) if watcher else None,
            on_status=self._record,
            stall_timeout_s=kw.pop("stall_timeout_s", 0.15),
            confirm_timeout_s=0.3,
            backoff=Backoff(initial_s=0.02, max_s=0.1),
            **kw,
        )
        self._running = True
        self._pump = threading.Thread(target=self._run, daemon=True)

    def _record(self, status: SourceStatus) -> None:
        with self._lock:
            self.events.append(status)

    def _run(self) -> None:
        while self._running:
            with self.src._lock:  # like PortAudio: no callbacks while a stream is swapped
                self.sd.pump()
            time.sleep(0.005)

    def start(self) -> "Rig":
        self.src.start()
        self._pump.start()
        wait(
            lambda: self.src._supervisor is not None and len(self.src._supervisor._last_frame) == 2
        )
        return self

    def stop(self) -> None:
        self._running = False
        self._pump.join()

    def stream(self, channel: Channel) -> Any:
        return self.src._streams[channel]

    def kinds(self, channel: Channel) -> list[str]:
        with self._lock:
            return [e.kind for e in self.events if e.channel is channel]

    def last(self, channel: Channel) -> SourceStatus:
        with self._lock:
            return [e for e in self.events if e.channel is channel][-1]

    def restored(self, channel: Channel, n: int = 1) -> Callable[[], bool]:
        return lambda: self.kinds(channel).count("restored") >= n


def wait(pred: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


@pytest.fixture
def rigs() -> Iterator[list[Rig]]:
    made: list[Rig] = []
    yield made
    for r in made:
        r.stop()


def _rig(rigs: list[Rig], **kw: Any) -> Rig:
    r = Rig(**kw)
    rigs.append(r)
    return r.start()


async def test_unplugged_headset_falls_back_without_touching_system_audio(
    rigs: list[Rig],
) -> None:
    r = _rig(rigs, mic="USB Mic", default_input="USB Mic")
    assert r.src.mic_id == "USB Mic"
    system_stream = r.stream(OTHERS)
    frames_before = len(r.src._queue)
    r.sd.unplug("USB Mic")
    r.watcher.default_input = BUILTIN
    r.watcher.changed()
    wait(r.restored(ME))
    lost, restored = [e for e in r.events if e.channel is ME]
    assert "'USB Mic' disconnected" in lost.reason
    assert (restored.device, r.src.mic_id) == (BUILTIN, BUILTIN)
    assert restored.gap_s is not None and 0 < restored.gap_s < 2.0
    # The other channel kept flowing: same stream, never stopped, no events.
    assert r.stream(OTHERS) is system_stream and not system_stream.stopped
    assert r.kinds(OTHERS) == []
    assert r.sd.reinitialised == 1  # only the tap's, at start: no re-enumeration
    assert len(r.src._queue) > frames_before
    await r.src.close()


async def test_replugged_headset_is_picked_up_again(rigs: list[Rig]) -> None:
    r = _rig(rigs, mic="USB Mic", default_input="USB Mic")
    r.sd.unplug("USB Mic")
    r.watcher.changed()
    wait(r.restored(ME))
    r.sd.plug("USB Mic", inputs=2, rate=44100.0)
    r.watcher.changed()
    wait(r.restored(ME, 2))
    wait(r.restored(OTHERS))
    assert r.src.mic_id == "USB Mic"
    assert r.stream(ME).kwargs["samplerate"] == 44100
    me = [e for e in r.events if e.channel is ME]
    assert me[2].reason == "switching to microphone 'USB Mic'"
    # PortAudio had to re-enumerate to see it, which reopened system audio too.
    assert r.sd.reinitialised == 2
    assert r.kinds(OTHERS) == ["lost", "restored"]
    assert r.last(OTHERS).gap_s is not None and r.last(OTHERS).gap_s < 2.0  # type: ignore[operator]
    assert r.last(OTHERS).device == "Sombra system audio 0"
    assert len(r.taps) == 1 and not r.taps[0].destroyed  # the tap itself survives
    await r.src.close()


async def test_default_mic_follows_the_system_default(rigs: list[Rig]) -> None:
    r = _rig(rigs, default_input=BUILTIN)
    assert r.src.mic_id == BUILTIN
    r.sd.plug("AirPods", inputs=1, rate=24000.0)
    r.watcher.default_input = "AirPods"
    r.watcher.changed()
    wait(r.restored(ME))
    assert r.src.mic_id == "AirPods"
    r.sd.unplug("AirPods")
    r.watcher.default_input = BUILTIN
    r.watcher.changed()
    wait(r.restored(ME, 2))
    assert r.src.mic_id == BUILTIN
    assert r.sd.reinitialised == 2  # plugging needed a re-enumeration, unplugging did not
    await r.src.close()


async def test_output_switch_reopens_the_tap_stream_only(rigs: list[Rig]) -> None:
    r = _rig(rigs)
    mic_stream = r.stream(ME)
    r.watcher.default_output = "AirPods"
    r.watcher.changed()
    wait(r.restored(OTHERS))
    assert "'MacBook Pro Speakers' -> 'AirPods'" in r.events[0].reason
    assert r.stream(ME) is mic_stream and not mic_stream.stopped
    assert r.kinds(ME) == []
    assert r.sd.reinitialised == 1 and len(r.taps) == 1
    await r.src.close()


async def test_output_switch_is_ignored_on_the_loopback_path(rigs: list[Rig]) -> None:
    r = _rig(rigs, macos_release="14.2")
    assert r.src.system_path == "loopback:BlackHole 2ch"
    r.watcher.default_output = "AirPods"
    r.watcher.changed()
    await asyncio.sleep(0.3)
    assert r.events == []
    await r.src.close()


async def test_dead_tap_is_recreated_after_a_plain_reopen_fails(rigs: list[Rig]) -> None:
    r = _rig(rigs)
    r.sd.unplug("Sombra system audio 0")  # the aggregate stops delivering audio
    wait(r.restored(OTHERS))
    kinds = r.kinds(OTHERS)
    assert kinds[0] == "lost" and "no audio" in r.events[0].reason
    assert "retrying" in kinds  # attempt 1 reopened the dead aggregate: no audio came
    assert r.taps[0].destroyed and len(r.taps) == 2
    assert r.last(OTHERS).device == "Sombra system audio 1"
    wait(r.restored(ME))  # the mic rode along with the re-enumeration
    await r.src.close()
    assert r.taps[1].destroyed


async def test_stall_watchdog_escalates_without_notifications(rigs: list[Rig]) -> None:
    r = _rig(rigs, watcher=False, mic="USB Mic")
    r.sd.unplug("USB Mic")  # no Core Audio listener: only the silence tells
    wait(r.restored(ME), timeout=5)
    kinds = r.kinds(ME)
    assert kinds[0] == "lost" and kinds.count("retrying") >= 2
    # Attempts 1-2 reopened the stale "USB Mic"; attempt 3 re-enumerated and fell back.
    assert r.src.mic_id == BUILTIN
    assert r.sd.reinitialised >= 2
    await r.src.close()


async def test_no_microphone_retries_while_system_audio_flows(rigs: list[Rig]) -> None:
    r = _rig(rigs)
    system_stream = r.stream(OTHERS)
    for name in (BUILTIN, "USB Mic"):
        r.sd.unplug(name)
    r.watcher.changed()
    wait(lambda: r.kinds(ME).count("retrying") >= 3)
    assert "no microphone connected" in r.last(ME).reason
    assert r.stream(OTHERS) is system_stream and r.kinds(OTHERS) == []
    assert r.sd.reinitialised == 1
    r.sd.plug("USB Mic")
    r.watcher.changed()
    wait(r.restored(ME))
    assert r.src.mic_id == "USB Mic"
    await r.src.close()


async def test_a_mic_that_fails_to_open_falls_through_the_chain(rigs: list[Rig]) -> None:
    r = _rig(rigs, mic="USB Mic", default_input="USB Mic")
    r.sd.fail_open.add("USB Mic")
    r.sd.unplug("USB Mic")
    r.sd.plug("USB Mic")
    r.watcher.changed()  # "USB Mic" is live but cannot be opened
    wait(r.restored(ME))
    assert r.src.mic_id == BUILTIN
    await r.src.close()


async def test_other_channel_failing_after_reenumeration_is_retried(rigs: list[Rig]) -> None:
    r = _rig(rigs, mic="USB Mic")
    r.sd.unplug("USB Mic")
    r.watcher.changed()
    wait(r.restored(ME))
    r.sd.fail_open.add("Sombra system audio 0")
    r.sd.plug("USB Mic")
    r.watcher.changed()  # re-enumerates; the aggregate then refuses to open
    wait(r.restored(ME, 2))
    wait(lambda: "retrying" in r.kinds(OTHERS))
    assert "reopen after re-enumeration" in next(e for e in r.events if e.channel is OTHERS).reason
    r.sd.fail_open.clear()
    wait(r.restored(OTHERS))
    await r.src.close()


def _threads() -> list[str]:
    # asyncio.to_thread (used by close) keeps a pooled executor thread: not ours.
    return sorted(t.name for t in threading.enumerate() if not t.name.startswith("asyncio_"))


async def test_flapping_leaks_no_threads_or_streams(rigs: list[Rig]) -> None:
    baseline = _threads()
    r = _rig(rigs, mic="USB Mic", default_input="USB Mic")
    running = _threads()
    assert len(running) == len(baseline) + 2  # the reconnect worker and the test pump
    assert "sombra-audio-reconnect" in running
    for i in range(10):
        r.sd.unplug("USB Mic")
        r.watcher.changed()
        wait(r.restored(ME, 2 * i + 1))
        r.sd.plug("USB Mic", inputs=2, rate=44100.0)
        r.watcher.changed()
        wait(r.restored(ME, 2 * i + 2))
        assert _threads() == running
    wait(r.restored(OTHERS, 10))
    assert len(r.sd.open_streams()) == 2
    assert r.src.mic_id == "USB Mic"
    await r.src.close()
    assert r.sd.open_streams() == []
    assert r.watcher.stopped
    r.stop()
    assert _threads() == baseline


async def test_watcher_failure_falls_back_to_the_watchdog(
    rigs: list[Rig], caplog: pytest.LogCaptureFixture
) -> None:
    r = Rig()
    rigs.append(r)

    def broken() -> FakeWatcher:
        raise OSError("CoreAudio framework not found")

    r.src._watcher_factory = broken
    r.start()
    assert "no device-change notifications" in caplog.text
    r.sd.unplug(BUILTIN)
    wait(r.restored(ME), timeout=5)
    await r.src.close()


async def test_reconnect_can_be_disabled() -> None:
    sd = HotplugSounddevice()
    src = MacAudioSource(
        sounddevice=sd, tap_factory=lambda: FakeTap(sd), macos_release="14.5", reconnect=False
    )
    src.start()
    assert src._supervisor is None
    await src.close()


async def test_restart_after_close_is_refused(rigs: list[Rig]) -> None:
    r = _rig(rigs)
    await r.src.close()
    with pytest.raises(RuntimeError, match="closed"):
        r.src._restart_channel(ME, 1)


async def test_overflows_survive_a_reconnect(rigs: list[Rig]) -> None:
    r = _rig(rigs, mic="USB Mic", default_input="USB Mic")
    r.stream(ME).feed(0.02)  # the fake flags an overflow on the first block
    assert r.src.overflows == 1
    r.sd.unplug("USB Mic")
    r.watcher.changed()
    wait(r.restored(ME))
    assert r.src.overflows == 1
    await r.src.close()


async def test_watcher_that_disagrees_on_names_is_not_trusted(
    rigs: list[Rig], caplog: pytest.LogCaptureFixture
) -> None:
    r = Rig()
    rigs.append(r)
    r.watcher.input_names = lambda: {"Built-in Microphone"}  # type: ignore[method-assign]
    r.start()
    assert r.src._watcher is None
    assert "does not list the open mic" in caplog.text
    await r.src.close()


async def test_quick_unplug_replug_is_caught_on_the_next_change(rigs: list[Rig]) -> None:
    r = _rig(rigs, mic="USB Mic", watcher=True, stall_timeout_s=5.0)
    r.sd.unplug("USB Mic")
    with r.src._lock:  # a notification seen while the headset is out...
        r.src._live_inputs(r.sd)
    r.sd.plug("USB Mic", inputs=2, rate=44100.0)
    r.watcher.changed()  # ...and the next one after it is back, on a new device id
    wait(r.restored(ME))
    assert "'USB Mic' disconnected" in r.events[0].reason
    assert r.sd.reinitialised == 2
    await r.src.close()


def test_fourcc() -> None:
    from sombra.audio._coreaudio_watch import PROP_DEVICES, fourcc

    assert PROP_DEVICES == 0x64657623
    with pytest.raises(ValueError, match="4 characters"):
        fourcc("dev")


async def test_chunk_stamps_stay_increasing_across_reconnects(rigs: list[Rig]) -> None:
    r = _rig(rigs, mic="USB Mic", default_input="USB Mic")
    for i in range(5):
        r.sd.unplug("USB Mic")
        r.watcher.changed()
        wait(r.restored(ME, 2 * i + 1))
        r.sd.plug("USB Mic", inputs=2, rate=44100.0)
        r.watcher.changed()
        wait(r.restored(ME, 2 * i + 2))
        wait(r.restored(OTHERS, i + 1))  # system audio was reopened by the re-init
    await r.src.close()
    starts: dict[Channel, list[Any]] = {ME: [], OTHERS: []}
    while (chunk := await r.src._queue.get()) is not None:
        starts[chunk.channel].append(chunk.start)
    for ch, seq in starts.items():
        assert len(seq) > 20
        backwards = [(b - a).total_seconds() for a, b in pairwise(seq) if b <= a]
        assert backwards == [], f"{ch.value} went backwards: {backwards}"
