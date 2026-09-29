"""Agent failures never stop recording; they are logged and shown to the user."""

from __future__ import annotations

from pathlib import Path

from orchestrator_harness import Rig, chatter, make_rig, speech, until

from fakes import FakeBrain, FakeUI, TriggerRule
from sombra.contracts import (
    AgentErrorLogged,
    Suggestion,
    SuggestionLogged,
    TimelineEntry,
    TriggerEvent,
)

RULES = [TriggerRule("nick")]


def script_with_questions(n: int = 60, at: tuple[int, ...] = (5,)) -> list[TimelineEntry]:
    lines = chatter(n)
    for i in at:
        lines[i] = speech(i, f"Nick, pergunta {i}?")
    return lines


def errors(rig: Rig) -> list[AgentErrorLogged]:
    return [e for e in rig.store.events if isinstance(e, AgentErrorLogged)]


async def test_brain_raising_keeps_recording(tmp_path: Path) -> None:
    rig = await make_rig(
        tmp_path, script=script_with_questions(), rules=RULES, brain=FakeBrain(fail_on={0})
    ).start()
    await until(lambda: len(rig.ui.failures) == 1, what="failure notice")
    at_failure, frames_at_failure = rig.speech_count(), len(rig.store.frames)
    await until(lambda: rig.transcriber.exhausted, what="rest of the meeting")
    await until(lambda: len(rig.store.frames) > frames_at_failure, what="more frames")
    await rig.stop()

    assert rig.speech_count() == 60 > at_failure
    (err,) = errors(rig)
    assert err.trigger_id == "t001"
    assert err.error == "RuntimeError: fake brain call 0 failed"
    trigger, reason = rig.ui.failures[0]
    assert trigger.id == "t001" and reason == err.error
    assert rig.ui.shown == []
    assert rig.store.log_types() == ["trigger", "agent_error"]


async def test_brain_timeout_is_a_failure_and_the_next_trigger_works(tmp_path: Path) -> None:
    rig = await make_rig(
        tmp_path,
        script=script_with_questions(at=(5, 30)),
        rules=RULES,
        brain=FakeBrain(hang_on={0}),
        settings={"brain_timeout_s": 0.05},
    ).start()
    await until(lambda: rig.transcriber.exhausted and len(rig.ui.shown) == 1, what="2nd answer")
    await rig.stop()

    (err,) = errors(rig)
    assert err.trigger_id == "t001" and err.error == "timeout: no answer after 0.05 s"
    assert [t.id for t, _ in rig.ui.failures] == ["t001"]
    assert rig.ui.shown[0].trigger_id == "t002"
    assert rig.speech_count() == 60
    assert rig.store.log_types() == ["trigger", "agent_error", "trigger", "suggestion"]


async def test_agent_that_fails_to_start_fails_each_trigger_not_the_meeting(
    tmp_path: Path,
) -> None:
    rig = await make_rig(
        tmp_path,
        script=script_with_questions(at=(5, 30)),
        rules=RULES,
        brain=FakeBrain(fail_start=True),
    ).start()
    await until(lambda: rig.transcriber.exhausted and len(rig.ui.failures) == 2, what="failures")
    await rig.stop()

    assert [e.error for e in errors(rig)] == ["RuntimeError: fake agent failed to start"] * 2
    assert rig.brain.requests == []
    assert rig.speech_count() == 60
    assert rig.brain.closed


class BrokenOverlay(FakeUI):
    def __init__(self, *, notify: bool = False, show: bool = False) -> None:
        super().__init__()
        self.break_notify, self.break_show = notify, show

    async def notify_trigger(self, trigger: TriggerEvent) -> None:
        if self.break_notify:
            raise RuntimeError("overlay gone")
        await super().notify_trigger(trigger)

    async def show(self, suggestion: Suggestion) -> None:
        if self.break_show:
            raise RuntimeError("overlay gone")
        await super().show(suggestion)


async def test_notify_trigger_failure_does_not_block_the_answer(tmp_path: Path) -> None:
    rig = await make_rig(
        tmp_path, script=script_with_questions(20), rules=RULES, ui=BrokenOverlay(notify=True)
    ).start()
    await until(lambda: len(rig.ui.shown) == 1, what="answer")
    await rig.stop()
    assert any(isinstance(e, SuggestionLogged) for e in rig.store.events)
    assert errors(rig) == []


async def test_overlay_failure_is_logged_as_error(tmp_path: Path) -> None:
    rig = await make_rig(
        tmp_path, script=script_with_questions(20), rules=RULES, ui=BrokenOverlay(show=True)
    ).start()
    await until(lambda: len(rig.ui.failures) == 1, what="failure notice")
    await rig.stop()
    (err,) = errors(rig)
    assert err.error == "overlay: RuntimeError: overlay gone"
    assert not any(isinstance(e, SuggestionLogged) for e in rig.store.events)
