"""Crash isolation: one broken pipeline task is restarted; the others keep running."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from orchestrator_harness import chatter, make_rig, until

from fakes import FakeScreenSource, FakeTranscriber
from sombra.orchestrator import Supervised, supervisor
from sombra.orchestrator.session import AUDIO, SCREEN, TIMELINE, TRANSCRIBE, TRIGGERS


@pytest.mark.parametrize("victim", [SCREEN, AUDIO, TRANSCRIBE, TIMELINE, TRIGGERS])
async def test_killing_one_task_does_not_stop_the_others(tmp_path: Path, victim: str) -> None:
    rig = await make_rig(tmp_path, script=chatter(10_000)).start()
    await until(lambda: rig.speech_count() > 5 and len(rig.store.frames) > 2, what="capture")

    sup = rig.session.pipelines[victim]
    child = sup.current
    assert child is not None
    child.cancel()
    await until(lambda: sup.restarts == 1 and sup.current is not child, what="restart")

    speech, frames = rig.speech_count(), len(rig.store.frames)
    await until(lambda: rig.speech_count() > speech + 5, what="speech keeps flowing")
    await until(lambda: len(rig.store.frames) > frames, what="frames keep flowing")
    others = [p for name, p in rig.session.pipelines.items() if name != victim]
    assert all(p.restarts == 0 for p in others)
    await rig.stop()


async def test_exceptions_restart_only_the_failing_pipeline(tmp_path: Path) -> None:
    rig = await make_rig(
        tmp_path,
        script=chatter(40),
        transcriber=FakeTranscriber(chatter(40), fail_after=10),
        screen=FakeScreenSource(fail_on={3, 4}),
    ).start()
    await until(lambda: rig.transcriber.exhausted, what="script")
    await until(lambda: len(rig.store.frames) > 4, what="frames")
    await rig.stop()

    assert rig.session.pipelines[TRANSCRIBE].restarts == 1
    assert rig.session.pipelines[SCREEN].restarts == 2
    assert rig.session.pipelines[AUDIO].restarts == 0
    assert rig.transcriber.calls == 2
    # The line due when the transcriber crashed was lost; everything else was written.
    assert rig.speech_count() == 40


async def test_supervisor_does_not_restart_a_task_that_finished() -> None:
    runs = 0

    async def once() -> None:
        nonlocal runs
        runs += 1

    sup = Supervised("once", once, backoff_s=0.001, backoff_max_s=0.01)
    await asyncio.wait_for(sup.start(), 1)
    assert runs == 1 and sup.restarts == 0


async def test_supervisor_backoff_doubles_up_to_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    real_sleep = asyncio.sleep
    runs = 0

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    async def crash() -> None:
        nonlocal runs
        runs += 1
        if runs <= 5:
            raise ValueError("boom")

    monkeypatch.setattr("sombra.orchestrator.supervisor.asyncio.sleep", fake_sleep)
    sup = Supervised("crashy", crash, backoff_s=1.0, backoff_max_s=4.0)
    await asyncio.wait_for(sup.start(), 1)
    assert delays == [1.0, 2.0, 4.0, 4.0, 4.0]
    assert sup.restarts == 5 and runs == 6


async def test_supervisor_resets_backoff_after_a_long_healthy_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    real_sleep = asyncio.sleep
    clock = iter([0.0, 0.0, 0.0, 10.0, 10.0])  # the 2nd run lasts 10 s > backoff cap
    runs = 0

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    async def crash() -> None:
        nonlocal runs
        runs += 1
        if runs <= 2:
            raise ValueError("boom")

    monkeypatch.setattr("sombra.orchestrator.supervisor.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(supervisor, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    sup = Supervised("crashy", crash, backoff_s=1.0, backoff_max_s=4.0)
    await asyncio.wait_for(sup.start(), 1)
    assert delays == [1.0, 1.0]
