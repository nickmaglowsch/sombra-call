"""End to end: ``sombra replay`` on a synthetic PT-BR meeting with the real models.

Silero VAD + whisper.cpp ``tiny`` transcribe two WAVs (eSpeak voices, see
``tests/fixtures/e2e/make_fixture.py``); a slide screenshot goes through dedupe; the
real trigger detector must notice "Maria, você pode explicar este gráfico na tela?"
(or its second phrasing; the cooldown lets one fire); the
scripted brain answers with the slide attached and the auto-approving UI approves.
Only the model API and the overlay window are stand-ins.

Runs in CI: the workflow caches ``~/.cache/sombra/models``, and the first run downloads
the models (~80 MB). Outside CI a machine that cannot reach the model hosts skips this
test; in CI an unreachable host is a failure, as in ``tests/transcription``.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
from pathlib import Path

import pytest

from sombra.contracts import Channel, FrameMarker, SpeechLine, parse_line
from sombra.orchestrator.replay import (
    AutoApproveUI,
    ReplayOptions,
    ScriptedBrain,
    run_replay,
)
from sombra.store import read_started_at
from sombra.transcription import SILERO_VAD, ensure_model, whisper_model

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "e2e"
USER = "Maria"


@pytest.fixture(scope="module")
def models_dir() -> Path:
    try:
        ensure_model(SILERO_VAD)
        return ensure_model(whisper_model("tiny")).parent
    except (urllib.error.URLError, OSError) as e:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"cannot download the models here ({e}); CI runs this test")


@pytest.mark.slow
async def test_replay_ptbr_meeting_triggers_and_approves_with_the_slide(
    tmp_path: Path, models_dir: Path
) -> None:
    brain, ui = ScriptedBrain(), AutoApproveUI()
    opts = ReplayOptions(
        me=FIXTURE / "me.wav",
        others=FIXTURE / "others.wav",
        frames=FIXTURE / "frames",
        meetings_root=tmp_path,
        user_name=USER,
        name="e2e",
        stt_model="tiny",
        models_dir=models_dir,
        n_threads=2,
        use_gpu=False,
    )
    meeting = await asyncio.wait_for(run_replay(opts, brain=brain, ui=ui), timeout=300)

    raw = (meeting / "transcript.md").read_text(encoding="utf-8")
    heard = f"tiny heard:\n{raw}"
    day = read_started_at(meeting)
    entries = [parse_line(x, day=day) for x in raw.splitlines() if x.strip()]
    speech = [e for e in entries if isinstance(e, SpeechLine)]
    markers = [e for e in entries if isinstance(e, FrameMarker)]

    # Transcript: both channels, and the slide once (its duplicate was deduped).
    assert any(e.channel is Channel.ME for e in speech), heard
    assert sum(e.channel is Channel.OTHERS for e in speech) >= 2, heard
    assert [(m.frame_id, m.window_title) for m in markers] == [("f0001", "Zoom - Vendas Q3")], heard
    assert entries.index(markers[0]) < entries.index(speech[-1]), heard

    # log.jsonl: trigger -> suggestion (slide attached) -> action.
    events = [json.loads(x) for x in (meeting / "log.jsonl").read_text().splitlines()]
    assert [e["type"] for e in events] == ["trigger", "suggestion", "action"], heard
    trigger, suggestion, action = events
    assert trigger["matched_alias"] == USER, heard
    assert trigger["needs_screen"] is True, heard
    assert suggestion["trigger_id"] == trigger["trigger_id"]
    assert suggestion["frames_sent"] == ["f0001"]
    assert action["suggestion_id"] == suggestion["suggestion_id"]
    assert action["kind"] == "approve"
    assert action["final_text"] == suggestion["text"]

    # The brain really got the kept frame file, read-only under the meeting folder.
    (request,) = brain.requests
    assert [p.relative_to(meeting).as_posix() for p in request.frame_paths] == ["frames/f0001.jpg"]
    assert ui.failures == []
