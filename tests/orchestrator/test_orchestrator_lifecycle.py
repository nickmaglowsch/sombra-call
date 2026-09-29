"""run()/stop(): draining, minutes, closing ports, Ctrl+C, epochs and settings."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from orchestrator_harness import chatter, make_rig, speech, until

from fakes import FakeAudioSource, FakeBrain, FakeTranscriber, TriggerRule
from sombra.contracts import AutonomyLevel, SummaryEpochLogged, Usage
from sombra.orchestrator import EpochSummary, SessionSettings


async def test_stop_drains_pending_work_then_writes_minutes_and_closes(tmp_path: Path) -> None:
    calls: list[str] = []

    async def minutes() -> None:
        calls.append(f"minutes after {len(rig.ui.shown)} answers")

    script = chatter(6)
    script[1] = speech(1, "Nick, primeira?")
    script[2] = speech(2, "Nick, segunda?")
    rig = make_rig(
        tmp_path,
        script=script,
        rules=[TriggerRule("nick")],
        brain=FakeBrain(latency_s=0.1),
        minutes=minutes,
    )
    await rig.start()
    await until(lambda: len(rig.brain.requests) == 1 and rig.transcriber.exhausted, what="busy")
    await rig.stop()  # one answer running, one pending: both finish before shutdown

    assert [s.trigger_id for s in rig.ui.shown] == ["t001", "t002"]
    assert calls == ["minutes after 2 answers"]
    assert rig.audio.closed and rig.screen.closed and rig.brain.closed
    assert all(p.task is not None and p.task.done() for p in rig.session.pipelines.values())


async def test_stop_is_idempotent_and_run_only_once(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, script=chatter(3)).start()
    await rig.stop()
    await rig.session.stop()
    with pytest.raises(RuntimeError, match="runs once"):
        await rig.session.run()


async def test_stop_before_run_makes_run_return_immediately(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    await rig.session.stop()
    await asyncio.wait_for(rig.session.run(), 2)
    assert rig.audio.closed and rig.screen.closed


async def test_stop_drains_audio_already_queued(tmp_path: Path) -> None:
    # The transcriber is slower than capture, so chunks pile up in the audio queue.
    rig = make_rig(
        tmp_path,
        script=chatter(50),
        audio=FakeAudioSource(count=50),
        transcriber=FakeTranscriber(chatter(50)),
    )
    await rig.start()
    await until(lambda: rig.audio.yielded == 50, what="all audio captured")
    await rig.stop()
    assert rig.speech_count() == 50


async def test_ctrl_c_cancels_run_and_still_shuts_down_cleanly(tmp_path: Path) -> None:
    ran: list[bool] = []

    async def minutes() -> None:
        ran.append(True)

    rig = await make_rig(tmp_path, script=chatter(100), minutes=minutes).start()
    await until(lambda: rig.speech_count() > 3, what="capture")
    assert rig.task is not None
    rig.task.cancel()  # what asyncio.run does on KeyboardInterrupt
    with pytest.raises(asyncio.CancelledError):
        await rig.task
    assert ran == [True]
    assert rig.audio.closed and rig.screen.closed and rig.brain.closed


async def test_stop_gives_up_on_a_hung_answer_after_the_drain_timeout(tmp_path: Path) -> None:
    script = chatter(4)
    script[1] = speech(1, "Nick?")
    rig = await make_rig(
        tmp_path,
        script=script,
        rules=[TriggerRule("nick")],
        brain=FakeBrain(hang_on={0}),
        settings={"brain_timeout_s": 60, "drain_timeout_s": 0.1},
    ).start()
    await until(lambda: len(rig.brain.requests) == 1, what="hung call")
    started = time.monotonic()
    await rig.stop()
    assert time.monotonic() - started < 1
    assert rig.brain.closed


async def test_failing_ports_do_not_break_shutdown(tmp_path: Path) -> None:
    async def minutes() -> None:
        raise OSError("disk full")

    rig = await make_rig(tmp_path, script=chatter(3), minutes=minutes).start()

    async def broken_close() -> None:
        raise RuntimeError("device gone")

    rig.audio.close = broken_close  # type: ignore[method-assign]
    await rig.stop()
    assert rig.screen.closed and rig.brain.closed


async def test_epochs_call_summarizer_then_hook_and_log(tmp_path: Path) -> None:
    order: list[str] = []
    fail_once = [True]

    async def summarize(n: int) -> EpochSummary:
        if fail_once and n == 2:
            fail_once.clear()
            raise RuntimeError("summary API down")
        order.append(f"summary {n}")
        return EpochSummary(model="haiku", usage=Usage(output_tokens=n))

    rig = await make_rig(
        tmp_path,
        script=chatter(1000),
        autonomy=AutonomyLevel.L0,
        settings={"epoch_interval_s": 0.03},
        summarizer=summarize,
        epoch_hook=lambda n: order.append(f"epoch {n}"),
    ).start()
    await until(lambda: rig.session.epoch >= 3, what="3 epochs")
    await rig.stop()

    # Epoch 2 failed once and was retried: numbering has no gaps and the hook only
    # moves after its summary exists (the prefix stays stable until then).
    assert order[:6] == ["summary 1", "epoch 1", "summary 2", "epoch 2", "summary 3", "epoch 3"]
    logged = [e for e in rig.store.events if isinstance(e, SummaryEpochLogged)]
    assert [e.epoch for e in logged][:3] == [1, 2, 3]
    assert logged[0].model == "haiku" and logged[0].usage.output_tokens == 1
    assert rig.session.pipelines["epochs"].restarts == 1


async def test_no_epoch_task_without_a_summarizer(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path).start()
    assert "epochs" not in rig.session.pipelines
    await rig.stop()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"screen_interval_s": 0},
        {"brain_timeout_s": -1},
        {"queue_size": 0},
        {"max_frames": 4},
        {"max_frames": -1},
        {"restart_backoff_s": 2.0, "restart_backoff_max_s": 1.0},
    ],
)
def test_settings_are_validated(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        SessionSettings(**kwargs)  # type: ignore[arg-type]


def test_default_settings_match_the_prd() -> None:
    s = SessionSettings()
    assert 5 <= s.screen_interval_s <= 10
    assert s.max_frames == 3
    assert s.autonomy is AutonomyLevel.L2


async def test_hung_minutes_and_close_do_not_hang_stop(tmp_path: Path) -> None:
    async def hang() -> None:
        await asyncio.Event().wait()

    rig = await make_rig(
        tmp_path,
        script=chatter(3),
        minutes=hang,
        settings={"minutes_timeout_s": 0.05, "close_timeout_s": 0.05},
    ).start()
    rig.audio.close = hang  # type: ignore[method-assign]
    started = time.monotonic()
    await rig.stop()
    assert time.monotonic() - started < 1
    assert rig.screen.closed and rig.brain.closed
