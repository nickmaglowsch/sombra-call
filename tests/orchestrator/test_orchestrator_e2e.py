"""Scripted 10-minute meeting, end to end against the fakes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from orchestrator_harness import BASE, chatter, make_rig, speech, until

from fakes import FakeAudioSource, TriggerRule
from sombra.contracts import ActionKind, Channel, FrameMarker, SpeechLine, Usage
from sombra.orchestrator import EpochSummary

Q1 = "Nick, o que você acha do prazo de sexta?"
Q2 = "Nick, o que você acha desse gráfico aqui?"
Q3 = "Nik, consegue resumir o que a gente decidiu?"  # Whisper's typical misspelling


def meeting_script() -> list[SpeechLine]:
    """40 utterances, one every 15 s: 10 minutes of meeting with 3 questions for Nick."""
    lines = [e for e in chatter(40) if isinstance(e, SpeechLine)]
    lines[10] = speech(10, Q1)
    lines[20] = speech(20, Q2)
    lines[30] = speech(30, Q3)
    return lines


async def test_ten_minute_meeting_with_three_triggers(tmp_path: Path) -> None:
    epochs: list[int] = []
    hook: list[int] = []

    async def summarize(n: int) -> EpochSummary:
        epochs.append(n)
        return EpochSummary(model="fake-summary", usage=Usage(input_tokens=10))

    def meeting_clock() -> datetime:
        # Suggestions appear 2 s (meeting time) after the question ended. Anchor on
        # the question the brain is answering, not on the last entry fed to the
        # detector: the timeline keeps flowing during the answer, and screen markers
        # carry wall-clock timestamps, so that would leak real scheduling into the
        # latency (#73). Answers run one at a time, so the last request is this one.
        asked = rig.brain.requests[-1].trigger.ts if rig.brain.requests else BASE
        return asked + timedelta(seconds=2)

    script = meeting_script()
    rig = make_rig(
        tmp_path,
        script=script,
        rules=[
            TriggerRule("desse gráfico", needs_screen=True),
            TriggerRule("nick,"),
            TriggerRule("nik,", alias="Nik"),
        ],
        audio=FakeAudioSource(interval_s=0.01),
        settings={"epoch_interval_s": 0.1},
        summarizer=summarize,
        epoch_hook=hook.append,
        clock=meeting_clock,
    )
    rig.ui.script = [
        (ActionKind.APPROVE, None),
        (ActionKind.EDIT, "sexta fecha, mas o gráfico precisa do Q3"),
        (ActionKind.DISCARD, None),
    ]

    await rig.start()
    await until(lambda: rig.transcriber.exhausted, what="script")
    await until(lambda: rig.store.log_types().count("action") == 3, what="3 actions")
    await rig.stop()

    # The whole script reached the transcript, interleaved with screen markers.
    speech_lines = [e for e in rig.store.entries if isinstance(e, SpeechLine)]
    assert speech_lines == script
    markers = [e for e in rig.store.entries if isinstance(e, FrameMarker)]
    assert [m.frame_id for m in markers] == [f.id for f in rig.store.frames]
    assert len(markers) >= 3

    # log.jsonl: per trigger, trigger -> suggestion -> action; epochs interleave.
    lines = [json.loads(line) for line in rig.store.log_lines()]
    flow = [ev for ev in lines if ev["type"] != "summary_epoch"]
    assert [ev["type"] for ev in flow] == ["trigger", "suggestion", "action"] * 3
    triggers, suggestions, actions = flow[0::3], flow[1::3], flow[2::3]
    assert [t["question"] for t in triggers] == [Q1, Q2, Q3]
    assert [t["needs_screen"] for t in triggers] == [False, True, False]
    assert [t["matched_alias"] for t in triggers] == ["Nick", "Nick", "Nik"]
    assert [s["trigger_id"] for s in suggestions] == [t["trigger_id"] for t in triggers]
    assert [a["suggestion_id"] for a in actions] == [s["suggestion_id"] for s in suggestions]
    assert [a["kind"] for a in actions] == ["approve", "edit", "discard"]
    assert actions[0]["final_text"] == suggestions[0]["text"]
    assert actions[1]["final_text"] == "sexta fecha, mas o gráfico precisa do Q3"
    assert actions[2]["final_text"] is None
    assert all(s["latency_ms"] == 2000 for s in suggestions)
    assert all(s["backend"] == "fake" and s["model"] == "fake-model" for s in suggestions)

    # Frames reached the brain only for the screen question: the last kept ones, max 3.
    req1, req2, req3 = rig.brain.requests
    assert req1.frame_paths == () and req3.frame_paths == ()
    event2 = rig.detector.fired[1]
    assert 1 <= len(req2.frame_paths) <= 3
    assert [p.stem for p in req2.frame_paths] == list(event2.candidate_frames)
    assert all(p.is_file() and p.parent == tmp_path / "frames" for p in req2.frame_paths)
    assert suggestions[1]["frames_sent"] == list(event2.candidate_frames)
    assert suggestions[0]["frames_sent"] == []

    # The overlay got the early notice, then the suggestion with its excerpt.
    assert [t.id for t in rig.ui.notified] == [t["trigger_id"] for t in triggers]
    assert rig.ui.shown[1].excerpt.endswith(f"OUTROS: {Q2}")
    assert rig.ui.failures == []

    # Summary epochs ran in order and the prefix builder heard about each one.
    epoch_events = [ev for ev in lines if ev["type"] == "summary_epoch"]
    assert epochs and hook == epochs == list(range(1, len(epochs) + 1))
    assert [ev["epoch"] for ev in epoch_events] == epochs
    assert all(ev["model"] == "fake-summary" for ev in epoch_events)

    # Every port was closed.
    assert rig.audio.closed and rig.screen.closed and rig.brain.closed
    assert rig.brain.meeting_dir == tmp_path
    assert Channel.OTHERS in {e.channel for e in speech_lines}
