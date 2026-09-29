"""ReconnectSupervisor with a fake device backend: deterministic clock, ``step()`` by hand."""

import threading
import time
from collections.abc import Mapping
from datetime import datetime

import pytest

from sombra.audio.clock import NS, MonotonicClock
from sombra.audio.reconnect import Backoff, ReconnectSupervisor, SourceStatus
from sombra.contracts import Channel

ME, OTHERS = Channel.ME, Channel.OTHERS


class FakeClock(MonotonicClock):
    def __init__(self) -> None:
        self.t = 1_000 * NS
        super().__init__(mono_ns=lambda: self.t, wall=lambda: datetime(2026, 9, 29, 14, 30))

    def advance(self, seconds: float) -> None:
        self.t += int(seconds * NS)


class FakeBackend:
    """Devices that fail to open ``failures[channel]`` times, then work."""

    def __init__(self) -> None:
        self.failures: dict[Channel, int] = {}
        self.calls: list[tuple[Channel, int]] = []
        self.reopen_both = False

    def restart(self, channel: Channel, attempt: int) -> Mapping[Channel, str]:
        self.calls.append((channel, attempt))
        if self.failures.get(channel, 0) > 0:
            self.failures[channel] -= 1
            raise OSError(f"{channel.value} device busy")
        if self.reopen_both:
            return {ME: "Built-in", OTHERS: "tap"}
        return {channel: f"{channel.value}-dev"}


def _sup(
    backend: FakeBackend, clock: FakeClock, **kw: object
) -> tuple[ReconnectSupervisor, list[SourceStatus]]:
    events: list[SourceStatus] = []
    sup = ReconnectSupervisor(
        backend.restart,
        on_status=events.append,
        clock=clock,
        stall_timeout_s=0.5,
        confirm_timeout_s=1.0,
        **kw,  # type: ignore[arg-type]
    )
    return sup, events


def _both_flowing(sup: ReconnectSupervisor) -> None:
    sup.frame(ME)
    sup.frame(OTHERS)


def test_backoff_doubles_up_to_the_cap() -> None:
    b = Backoff()
    assert [b.delay_s(n) for n in range(1, 7)] == [0.1, 0.2, 0.4, 0.8, 1.6, 2.0]
    # the first four retries fit the 2 s restart budget
    assert sum(b.delay_s(n) for n in range(1, 5)) < 2.0


def test_loss_retry_with_backoff_then_restore() -> None:
    clock, backend = FakeClock(), FakeBackend()
    backend.failures[ME] = 2
    sup, events = _sup(backend, clock)
    _both_flowing(sup)
    sup.report_lost(ME, "headset unplugged")
    sup.step()  # attempt 1 fails
    assert [(e.kind, e.attempt) for e in events] == [("lost", 0), ("retrying", 1)]
    assert events[0].reason == "headset unplugged"
    assert "busy" in events[1].reason
    clock.advance(0.05)
    sup.step()  # still inside the 0.1 s backoff
    assert backend.calls == [(ME, 1)]
    clock.advance(0.06)
    sup.step()  # attempt 2 fails, next backoff 0.2 s
    clock.advance(0.15)
    sup.step()
    assert backend.calls == [(ME, 1), (ME, 2)]
    clock.advance(0.06)
    sup.step()  # attempt 3 opens the device
    assert backend.calls[-1] == (ME, 3)
    assert sup.state(ME) == "confirming"
    clock.advance(0.03)
    sup.frame(ME)  # first audio from the new device
    sup.step()
    restored = events[-1]
    assert (restored.kind, restored.channel, restored.device) == ("restored", ME, "EU-dev")
    assert restored.gap_s == pytest.approx(0.05 + 0.06 + 0.15 + 0.06 + 0.03, abs=1e-6)
    assert restored.reason == ""
    assert sup.state(ME) == "running"
    # the other channel never noticed
    assert all(e.channel is ME for e in events)
    assert all(c[0] is ME for c in backend.calls)
    assert sup.state(OTHERS) == "running"


def test_stall_watchdog_is_armed_by_the_first_frame() -> None:
    clock, backend = FakeClock(), FakeBackend()
    sup, events = _sup(backend, clock)
    clock.advance(5)
    sup.step()  # no frame yet: a device that never started is not a reconnect
    assert events == []
    sup.frame(OTHERS)
    clock.advance(0.4)
    sup.step()
    assert events == []
    clock.advance(0.2)
    sup.step()
    assert [(e.channel, e.kind) for e in events] == [(OTHERS, "lost")]
    assert events[0].reason.startswith("no audio for 0.6 s")
    assert backend.calls == [(OTHERS, 1)]


def test_restart_without_audio_is_a_failed_attempt() -> None:
    clock, backend = FakeClock(), FakeBackend()
    sup, events = _sup(backend, clock)
    _both_flowing(sup)
    sup.report_lost(OTHERS, "default output changed")
    sup.step()
    clock.advance(1.1)  # opened, but not one frame within confirm_timeout
    sup.frame(ME)  # the mic keeps flowing meanwhile
    sup.step()
    assert events[-1].kind == "retrying"
    assert events[-1].reason == "no audio after restart"
    clock.advance(0.1)
    sup.frame(ME)
    sup.step()
    assert backend.calls == [(OTHERS, 1), (OTHERS, 2)]


def test_reopening_both_channels_confirms_the_other_one_too() -> None:
    clock, backend = FakeClock(), FakeBackend()
    backend.reopen_both = True
    sup, events = _sup(backend, clock)
    _both_flowing(sup)
    sup.report_lost(ME, "switching to microphone 'USB Mic'")
    sup.step()
    assert [(e.channel, e.kind) for e in events] == [(ME, "lost"), (OTHERS, "lost")]
    assert events[1].reason == "reopened with EU"
    clock.advance(0.02)
    _both_flowing(sup)
    sup.step()
    assert {(e.channel, e.kind) for e in events[2:]} == {(ME, "restored"), (OTHERS, "restored")}
    other = next(e for e in events[2:] if e.channel is OTHERS)
    assert other.gap_s == pytest.approx(0.02)
    assert other.device == "tap"


def test_repeated_notifications_coalesce() -> None:
    clock, backend = FakeClock(), FakeBackend()
    backend.failures[ME] = 1
    sup, events = _sup(backend, clock)
    for _ in range(5):
        sup.report_lost(ME, "gone")
    sup.step()
    assert backend.calls == [(ME, 1)]
    assert [e.kind for e in events] == ["lost", "retrying"]
    sup.report_lost(ME, "gone again")  # already lost: waits for the scheduled retry
    sup.step()
    assert backend.calls == [(ME, 1)]


def test_lost_again_while_confirming_retries_now() -> None:
    clock, backend = FakeClock(), FakeBackend()
    sup, events = _sup(backend, clock)
    sup.report_lost(ME, "gone")
    sup.step()
    sup.report_lost(ME, "gone again")
    sup.step()
    assert backend.calls == [(ME, 1), (ME, 2)]
    assert [e.kind for e in events] == ["lost"]


def test_check_runs_after_poke_and_unknown_channels_are_ignored() -> None:
    clock, backend = FakeClock(), FakeBackend()
    found: list[tuple[Channel, str]] = [(ME, "mic unplugged")]
    sup = ReconnectSupervisor(backend.restart, [ME], check=lambda: found, clock=clock)
    sup.step()
    assert backend.calls == []  # check only runs when poked
    sup.poke()
    sup.step()
    assert backend.calls == [(ME, 1)]
    sup.report_lost(OTHERS, "not watched")
    sup.step()
    assert backend.calls == [(ME, 1)]


def test_hook_and_check_errors_do_not_stop_reconnecting(caplog: pytest.LogCaptureFixture) -> None:
    clock, backend = FakeClock(), FakeBackend()

    def bad_hook(_: SourceStatus) -> None:
        raise RuntimeError("ui gone")

    def bad_check() -> list[tuple[Channel, str]]:
        raise RuntimeError("core audio hiccup")

    sup = ReconnectSupervisor(backend.restart, check=bad_check, on_status=bad_hook, clock=clock)
    sup.poke()
    sup.report_lost(ME, "gone")
    sup.step()
    assert backend.calls == [(ME, 1)]
    assert "status hook failed" in caplog.text
    assert "device check failed" in caplog.text


def _wait(pred: object, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():  # type: ignore[operator]
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def test_worker_thread_flapping_leaks_no_threads() -> None:
    backend = FakeBackend()
    events: list[SourceStatus] = []
    sup = ReconnectSupervisor(backend.restart, on_status=events.append, tick_s=0.005)
    before = threading.active_count()
    sup.start()
    sup.start()  # idempotent
    assert threading.active_count() == before + 1
    for i in range(30):
        ch = ME if i % 2 else OTHERS
        sup.report_lost(ch, "flap")
        _wait(lambda ch=ch: sup.state(ch) == "confirming")
        sup.frame(ch)
        _wait(lambda ch=ch: sup.state(ch) == "running")
        assert threading.active_count() == before + 1
    assert sum(e.kind == "restored" for e in events) == 30
    sup.stop()
    sup.stop()
    assert threading.active_count() == before


def test_worker_survives_a_crashing_step(caplog: pytest.LogCaptureFixture) -> None:
    calls = 0

    def restart(channel: Channel, attempt: int) -> Mapping[Channel, str]:
        nonlocal calls
        calls += 1
        return {channel: "x"}

    sup = ReconnectSupervisor(restart, tick_s=0.005)
    original = sup.step
    crashed = threading.Event()

    def step() -> None:
        if not crashed.is_set():
            crashed.set()
            raise RuntimeError("boom")
        original()

    sup.step = step  # type: ignore[method-assign]
    sup.start()
    _wait(crashed.is_set)
    sup.report_lost(ME, "gone")
    _wait(lambda: calls == 1)
    sup.stop()
    assert "reconnect step failed" in caplog.text
