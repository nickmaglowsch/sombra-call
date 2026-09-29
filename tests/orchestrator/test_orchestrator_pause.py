"""Pause stops audio and screen writes within 200 ms; resume restores them."""

from __future__ import annotations

import asyncio
from pathlib import Path

from orchestrator_harness import chatter, make_rig, until

from fakes import FakeAudioSource

PAUSE_BUDGET_S = 0.2


async def test_pause_stops_writes_and_resume_restores_them(tmp_path: Path) -> None:
    rig = await make_rig(
        tmp_path, script=chatter(10_000), audio=FakeAudioSource(interval_s=0.002)
    ).start()
    await until(lambda: rig.speech_count() > 5 and len(rig.store.frames) > 2, what="capture")

    rig.pause.pause()
    assert rig.session.paused
    await asyncio.sleep(PAUSE_BUDGET_S)
    speech, frames, entries = rig.speech_count(), len(rig.store.frames), len(rig.store.entries)
    grabs = rig.screen.grabs
    await asyncio.sleep(0.3)
    assert rig.speech_count() == speech
    assert len(rig.store.frames) == frames
    assert len(rig.store.entries) == entries
    assert rig.screen.grabs == grabs  # the screen is not even captured while paused
    assert rig.session.dropped_paused > 0

    rig.pause.resume()
    await until(lambda: rig.speech_count() > speech + 5, within=1, what="speech after resume")
    await until(lambda: len(rig.store.frames) > frames, within=1, what="frames after resume")
    await rig.stop()
