"""Trigger handling: overhead, one-at-a-time queueing, frames, latency and actions."""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from orchestrator_harness import Rig, chatter, make_rig, speech, until

from fakes import FakeAudioSource, FakeBrain, FakeUI, TriggerRule
from sombra.contracts import (
    ActionKind,
    ActionLogged,
    SuggestionLogged,
    TimelineEntry,
    UserAction,
)

OVERHEAD_BUDGET_MS = 20.0


def questions(n: int, every: int = 3) -> list[TimelineEntry]:
    lines = chatter(n * every)
    for k in range(n):
        i = k * every + every - 1
        lines[i] = speech(i, f"Nick, pergunta {k}?")
    return lines


async def test_trigger_to_brain_overhead_is_under_20_ms(tmp_path: Path) -> None:
    rig = await make_rig(
        tmp_path,
        script=questions(30),
        rules=[TriggerRule("nick")],
        audio=FakeAudioSource(interval_s=0.005),
    ).start()
    await until(lambda: len(rig.ui.shown) == 30, what="30 answers")
    await rig.stop()

    overhead_ms = [
        (called - fired) * 1000
        for fired, called in zip(rig.detector.fired_at, rig.brain.called_at, strict=True)
    ]
    assert statistics.median(overhead_ms) < OVERHEAD_BUDGET_MS
    assert max(overhead_ms) < OVERHEAD_BUDGET_MS, overhead_ms


async def test_one_trigger_at_a_time_with_one_pending_and_the_rest_dropped(
    tmp_path: Path,
) -> None:
    script = chatter(10)
    for i in (2, 3, 4):  # three questions in a row while the first is still answering
        script[i] = speech(i, f"Nick, pergunta {i}?")
    rig = await make_rig(
        tmp_path, script=script, rules=[TriggerRule("nick")], brain=FakeBrain(latency_s=0.15)
    ).start()
    await until(lambda: len(rig.ui.shown) == 2 and rig.transcriber.exhausted, what="answers")
    await rig.stop()

    assert rig.brain.max_in_flight == 1
    assert [r.trigger.id for r in rig.brain.requests] == ["t001", "t002"]
    assert rig.session.dropped_triggers == 1
    assert rig.store.log_types().count("trigger") == 3  # the dropped one is still logged
    assert rig.store.log_types().count("suggestion") == 2


async def test_latency_is_overlay_shown_minus_end_of_question(tmp_path: Path) -> None:
    script = chatter(5)
    script[2] = speech(2, "Nick, e o deploy?")
    shown_at = script[2].ts + timedelta(milliseconds=4321)
    rig = await make_rig(
        tmp_path, script=script, rules=[TriggerRule("nick")], clock=lambda: shown_at
    ).start()
    await until(lambda: len(rig.ui.shown) == 1, what="answer")
    await rig.stop()

    (logged,) = [e for e in rig.store.events if isinstance(e, SuggestionLogged)]
    assert logged.latency_ms == 4321
    assert logged.ts == shown_at


def _frames_rig(tmp_path: Path, rule: TriggerRule, **kwargs: Any) -> Rig:
    script = chatter(40)
    script[39] = speech(39, "Nick, olha isso aqui")
    return make_rig(
        tmp_path,
        script=script,
        rules=[rule],
        audio=FakeAudioSource(interval_s=0.01),
        **kwargs,
    )


async def test_frames_only_when_needed_and_at_most_three(tmp_path: Path) -> None:
    rig = await _frames_rig(
        tmp_path,
        TriggerRule(
            "nick",
            needs_screen=True,
            candidate_frames=["f0004", "f0003", "f9999", "f0002", "f0001"],
        ),
    ).start()
    await until(lambda: len(rig.ui.shown) == 1, what="answer")
    await rig.stop()
    # Unknown f9999 is skipped; the list is capped at 3, most relevant first.
    assert [p.name for p in rig.brain.requests[0].frame_paths] == [
        "f0004.jpg",
        "f0003.jpg",
        "f0002.jpg",
    ]


async def test_no_frames_without_needs_screen(tmp_path: Path) -> None:
    rig = await _frames_rig(
        tmp_path, TriggerRule("nick", needs_screen=False, candidate_frames=["f0001"])
    ).start()
    await until(lambda: len(rig.ui.shown) == 1, what="answer")
    await rig.stop()
    assert rig.brain.requests[0].frame_paths == ()


async def test_frames_kept_before_the_session_resolve_from_disk(tmp_path: Path) -> None:
    old = tmp_path / "frames" / "f0100.jpg"
    old.parent.mkdir()
    old.write_bytes(b"jpeg")
    rig = await _frames_rig(
        tmp_path, TriggerRule("nick", needs_screen=True, candidate_frames=["f0100"])
    ).start()
    await until(lambda: len(rig.ui.shown) == 1, what="answer")
    await rig.stop()
    assert rig.brain.requests[0].frame_paths == (old,)


async def test_custom_frame_resolver(tmp_path: Path) -> None:
    rig = await _frames_rig(
        tmp_path,
        TriggerRule("nick", needs_screen=True, candidate_frames=["f0001"]),
        resolve_frame=lambda fid: Path("/elsewhere") / f"{fid}.png",
    ).start()
    await until(lambda: len(rig.ui.shown) == 1, what="answer")
    await rig.stop()
    assert rig.brain.requests[0].frame_paths == (Path("/elsewhere/f0001.png"),)


async def test_blocked_apps_are_never_kept(tmp_path: Path) -> None:
    rig = await make_rig(
        tmp_path,
        script=chatter(20),
        blocked=lambda app, title: app == "Zoom" or "senha" in (title or ""),
    ).start()
    await until(lambda: rig.screen.grabs > 10, what="grabs")
    await rig.stop()
    assert rig.frames.processed == [] and rig.store.frames == []


async def test_user_actions_are_logged(tmp_path: Path) -> None:
    ui = FakeUI()
    script = chatter(5)
    script[1] = speech(1, "Nick, bora?")
    rig = await make_rig(tmp_path, script=script, rules=[TriggerRule("nick")], ui=ui).start()
    await until(lambda: len(ui.shown) == 1, what="answer")
    sid = ui.shown[0].id
    ui.emit(UserAction(sid, ActionKind.APPROVE))
    ui.emit(UserAction(sid, ActionKind.APPROVE, "texto aprovado"))
    ui.emit(UserAction(sid, ActionKind.NOT_FOR_ME))
    ui.emit(UserAction("unknown", ActionKind.APPROVE))
    await until(lambda: rig.store.log_types().count("action") == 4, what="actions")
    await rig.stop()

    actions = [e for e in rig.store.events if isinstance(e, ActionLogged)]
    assert [(a.kind, a.final_text) for a in actions] == [
        (ActionKind.APPROVE, "resposta 1"),
        (ActionKind.APPROVE, "texto aprovado"),
        (ActionKind.NOT_FOR_ME, None),
        (ActionKind.APPROVE, None),
    ]
    assert all(isinstance(a.ts, datetime) for a in actions)
