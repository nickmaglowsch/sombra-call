"""``run_replay`` wiring on the e2e fixture, with a loudness VAD and scripted STT.

Everything but the models is real: FileAudioSource, DirectoryScreenSource, the
transcriber's VAD/STT threads, dedupe, NameTriggerDetector, MeetingStore and Session.
``tests/e2e`` runs the same flow with Silero + whisper ``tiny``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from sombra.contracts import AutonomyLevel, Channel, FrameMarker, SpeechLine, parse_line
from sombra.orchestrator.replay import (
    AutoApproveUI,
    PacedAudio,
    ReplayOptions,
    ReplayScreen,
    ScriptedBrain,
    run_replay,
)
from sombra.store import read_started_at
from sombra.transcription import WhisperTranscriber

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "e2e"
QUESTION = "Maria, você pode explicar este gráfico na tela?"
QUESTION_2 = "Maria, o que você acha desse gráfico aqui?"  # cooldown: no 2nd trigger
SCRIPT = {
    Channel.OTHERS: ["Vou compartilhar o gráfico de vendas do trimestre.", QUESTION, QUESTION_2],
    Channel.ME: ["Bom dia a todos."],
}


class Loudness:
    def __call__(self, frame: np.ndarray) -> float:
        return 0.9 if float(np.abs(frame).max()) > 0.02 else 0.02

    def reset(self) -> None:
        return None


class Lines:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)

    def transcribe(self, audio: np.ndarray) -> str:
        return self.texts.pop(0) if self.texts else ""


def fake_transcriber() -> WhisperTranscriber:
    return WhisperTranscriber(
        detector_factory=lambda _ch: Loudness(),
        stt_factory=lambda ch: Lines(SCRIPT[ch]),
    )


def options(tmp_path: Path, **kw: object) -> ReplayOptions:
    base: dict[str, object] = {
        "me": FIXTURE / "me.wav",
        "others": FIXTURE / "others.wav",
        "frames": FIXTURE / "frames",
        "meetings_root": tmp_path,
        "user_name": "Maria",
        "name": "Replay e2e",
    }
    return ReplayOptions(**{**base, **kw})  # type: ignore[arg-type]


def read_log(meeting: Path) -> list[dict[str, object]]:
    return [json.loads(x) for x in (meeting / "log.jsonl").read_text().splitlines()]


def timeline(meeting: Path) -> list[object]:
    lines = (meeting / "transcript.md").read_text(encoding="utf-8").splitlines()
    day = read_started_at(meeting)
    return [parse_line(x, day=day) for x in lines if x.strip()]


async def test_replay_writes_transcript_frames_trigger_suggestion_and_action(
    tmp_path: Path,
) -> None:
    brain, ui = ScriptedBrain(), AutoApproveUI()
    meeting = await run_replay(
        options(tmp_path), transcriber=fake_transcriber(), brain=brain, ui=ui
    )

    assert meeting.parent == tmp_path and meeting.name.endswith("_replay-e2e")
    entries = timeline(meeting)
    speech = [e for e in entries if isinstance(e, SpeechLine)]
    assert {(e.channel, e.text) for e in speech} == {
        (ch, text) for ch, texts in SCRIPT.items() for text in texts
    }
    markers = [e for e in entries if isinstance(e, FrameMarker)]
    assert [(m.frame_id, m.window_title) for m in markers] == [("f0001", "Zoom - Vendas Q3")]
    assert entries.index(markers[0]) < entries.index(next(e for e in speech if e.text == QUESTION))
    assert (meeting / "frames" / "f0001.jpg").is_file()
    index = (meeting / "frames" / "index.jsonl").read_text().splitlines()
    assert len(index) == 1  # the second screenshot is a duplicate

    events = read_log(meeting)
    assert [e["type"] for e in events] == ["trigger", "suggestion", "action"]
    trigger, suggestion, action = events
    assert trigger["question"] == QUESTION
    assert trigger["needs_screen"] is True
    assert trigger["matched_alias"] == "Maria"
    assert suggestion["trigger_id"] == trigger["trigger_id"]
    assert suggestion["frames_sent"] == ["f0001"]
    assert suggestion["backend"] == "scripted"
    assert action["suggestion_id"] == suggestion["suggestion_id"]
    assert action["kind"] == "approve"
    assert action["final_text"] == suggestion["text"]

    assert brain.meeting_dir == meeting
    assert [p.name for p in brain.requests[0].frame_paths] == ["f0001.jpg"]
    assert ui.failures == []


async def test_level_zero_logs_the_trigger_but_never_answers(tmp_path: Path) -> None:
    meeting = await run_replay(
        options(tmp_path, level=AutonomyLevel.L0), transcriber=fake_transcriber()
    )
    assert [e["type"] for e in read_log(meeting)] == ["trigger"]
    assert "L0" in (meeting / "meeting.toml").read_text()


async def test_replay_without_frames_or_brain_key(tmp_path: Path) -> None:
    meeting = await run_replay(
        options(tmp_path, frames=None, me=None),
        transcriber=fake_transcriber(),
        brain=ScriptedBrain(),
        ui=AutoApproveUI(),
    )
    events = read_log(meeting)
    assert [e["type"] for e in events] == ["trigger", "suggestion", "action"]
    assert events[0]["needs_screen"] is True  # "desse gráfico" ...
    assert events[1]["frames_sent"] == []  # ... but nothing was on screen
    assert not any(isinstance(e, FrameMarker) for e in timeline(meeting))

    with pytest.raises(ValueError, match="API key"):
        await run_replay(options(tmp_path), transcriber=fake_transcriber(), ui=AutoApproveUI())
    with pytest.raises(ValueError, match="ApprovalUI"):
        await run_replay(options(tmp_path), transcriber=fake_transcriber(), brain=ScriptedBrain())
    with pytest.raises(ValueError, match="L3"):
        await run_replay(options(tmp_path, level=AutonomyLevel.L3))


async def test_paced_replay_runs_at_speed(tmp_path: Path) -> None:
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    meeting = await run_replay(
        options(tmp_path, speed=20.0),
        transcriber=fake_transcriber(),
        brain=ScriptedBrain(),
        ui=AutoApproveUI(),
    )
    elapsed = loop.time() - t0
    assert 0.4 <= elapsed < 5.0  # ~11 s of audio at 20x
    events = read_log(meeting)
    assert [e["type"] for e in events] == ["trigger", "suggestion", "action"]
    assert events[1]["frames_sent"] == ["f0001"]


def test_paced_audio_rejects_a_bad_speed() -> None:
    with pytest.raises(ValueError, match="speed"):
        PacedAudio(None, 0)  # type: ignore[arg-type]


async def test_replay_screen_restamps_and_holds_after_the_last_frame(tmp_path: Path) -> None:
    from datetime import datetime

    from sombra.screen.replay import DirectoryScreenSource

    start = datetime(2026, 9, 29, 10, 0, 0).astimezone()
    screen = ReplayScreen(DirectoryScreenSource(FIXTURE / "frames"), start)
    first, second = await screen.grab(), await screen.grab()
    assert first.ts == start
    assert (second.ts - first.ts).total_seconds() == 2.0
    assert first.window_title == "Zoom - Vendas Q3"
    assert not screen.exhausted.is_set()
    held = asyncio.create_task(screen.grab())
    await asyncio.sleep(0.01)
    assert screen.exhausted.is_set() and not held.done()
    held.cancel()
    await screen.close()
    empty = ReplayScreen(None, start)
    assert empty.exhausted.is_set()
