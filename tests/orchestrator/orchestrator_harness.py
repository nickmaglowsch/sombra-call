"""Builds a Session wired to the shared fakes, plus polling helpers for the tests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fakes import (
    FakeAudioSource,
    FakeBrain,
    FakeFramePipeline,
    FakeScreenSource,
    FakeTranscriber,
    FakeUI,
    InMemoryStore,
    ScriptedTriggerDetector,
    TriggerRule,
)
from sombra.contracts import AutonomyLevel, Channel, SpeechLine, TimelineEntry
from sombra.orchestrator import PauseSwitch, Session, SessionSettings

BASE = datetime(2026, 9, 29, 14, 0, 0).astimezone()

FAST = {
    "screen_interval_s": 0.005,
    "brain_timeout_s": 2.0,
    "restart_backoff_s": 0.01,
    "restart_backoff_max_s": 0.05,
    "drain_timeout_s": 2.0,
}


def speech(i: int, text: str, channel: Channel = Channel.OTHERS, *, step_s: int = 15) -> SpeechLine:
    return SpeechLine(ts=BASE + timedelta(seconds=i * step_s), channel=channel, text=text)


def chatter(n: int, *, start: int = 0) -> list[TimelineEntry]:
    return [
        speech(start + i, f"fala {start + i}", Channel.ME if i % 2 else Channel.OTHERS)
        for i in range(n)
    ]


@dataclass
class Rig:
    session: Session
    audio: FakeAudioSource
    transcriber: FakeTranscriber
    screen: FakeScreenSource
    frames: FakeFramePipeline
    store: InMemoryStore
    detector: ScriptedTriggerDetector
    brain: FakeBrain
    ui: FakeUI
    pause: PauseSwitch
    task: asyncio.Task[None] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    async def start(self) -> Rig:
        self.task = asyncio.create_task(self.session.run())
        await asyncio.sleep(0)
        return self

    async def stop(self) -> None:
        await self.session.stop()
        assert self.task is not None
        await asyncio.wait_for(self.task, 5)

    def speech_count(self) -> int:
        return sum(isinstance(e, SpeechLine) for e in self.store.entries)


def make_rig(
    tmp_path: Path,
    *,
    script: Iterable[TimelineEntry] = (),
    rules: Iterable[TriggerRule] = (),
    autonomy: AutonomyLevel = AutonomyLevel.L2,
    audio: FakeAudioSource | None = None,
    transcriber: FakeTranscriber | None = None,
    screen: FakeScreenSource | None = None,
    brain: FakeBrain | None = None,
    ui: FakeUI | None = None,
    settings: dict[str, Any] | None = None,
    **session_kwargs: Any,
) -> Rig:
    store = InMemoryStore(tmp_path)
    pause = PauseSwitch()
    rig = Rig(
        session=None,  # type: ignore[arg-type]  # filled below
        audio=audio or FakeAudioSource(interval_s=0.002),
        transcriber=transcriber or FakeTranscriber(script),
        screen=screen or FakeScreenSource(),
        frames=FakeFramePipeline(tmp_path),
        store=store,
        detector=ScriptedTriggerDetector(rules),
        brain=brain or FakeBrain(),
        ui=ui or FakeUI(),
        pause=pause,
    )
    rig.session = Session(
        audio=rig.audio,
        transcriber=rig.transcriber,
        screen=rig.screen,
        frames=rig.frames,
        store=store,
        detector=rig.detector,
        brain=rig.brain,
        ui=rig.ui,
        settings=SessionSettings(autonomy=autonomy, **{**FAST, **(settings or {})}),
        pause=pause,
        **session_kwargs,
    )
    return rig


async def until(cond: Callable[[], bool], within: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + within
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.002)
