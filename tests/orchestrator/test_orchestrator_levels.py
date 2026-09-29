"""Autonomy levels: L0 records only; L1/L2 ask the brain and show the answer."""

from __future__ import annotations

from pathlib import Path

import pytest
from orchestrator_harness import chatter, make_rig, speech, until

from fakes import TriggerRule
from sombra.contracts import AutonomyLevel, TimelineEntry

RULES = [TriggerRule("nick")]


def script() -> list[TimelineEntry]:
    lines = chatter(30)
    lines[5] = speech(5, "Nick, e aí?")
    lines[20] = speech(20, "Nick, fecha na sexta?")
    return lines


async def test_l0_never_calls_the_brain(tmp_path: Path) -> None:
    minutes: list[str] = []

    async def write_minutes() -> None:
        minutes.append("ata")

    rig = await make_rig(
        tmp_path,
        script=script(),
        rules=RULES,
        autonomy=AutonomyLevel.L0,
        minutes=write_minutes,
    ).start()
    await until(lambda: rig.transcriber.exhausted and rig.speech_count() == 30, what="meeting")
    await rig.stop()

    assert rig.brain.requests == []
    assert not rig.brain.started and not rig.brain.closed
    assert rig.ui.notified == [] and rig.ui.shown == []
    # Triggers are still logged: they measure false triggers even in L0.
    assert rig.store.log_types() == ["trigger", "trigger"]
    assert minutes == ["ata"]


@pytest.mark.parametrize("level", [AutonomyLevel.L1, AutonomyLevel.L2])
async def test_l1_and_l2_call_the_brain(tmp_path: Path, level: AutonomyLevel) -> None:
    rig = await make_rig(tmp_path, script=script(), rules=RULES, autonomy=level).start()
    await until(lambda: len(rig.ui.shown) == 2, what="two answers")
    await rig.stop()

    assert [r.trigger.id for r in rig.brain.requests] == ["t001", "t002"]
    assert [t.id for t in rig.ui.notified] == ["t001", "t002"]
    assert rig.store.log_types() == ["trigger", "suggestion", "trigger", "suggestion"]
