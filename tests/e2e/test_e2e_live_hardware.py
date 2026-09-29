"""Live capture on a real Mac: mic + system audio + screen for ~20 s at L0 (manual run).

Needs macOS 14.4+, Screen Recording + Microphone permissions for the terminal, and the
default whisper model downloaded. Play some PT-BR speech on the speakers while it runs::

    uv run pytest -m hardware tests/e2e/test_e2e_live_hardware.py -s

The 30-minute acceptance run for #17 is ``sombra start`` by hand; see docs/usage.md.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytest

from sombra.config import UserConfig, UserIdentity
from sombra.contracts import AutonomyLevel
from sombra.orchestrator.live import LivePlan, StopSignal, run_live
from sombra.orchestrator.wiring import build_transcriber
from sombra.store import create_meeting

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.macos,
    pytest.mark.skipif(sys.platform != "darwin", reason="live capture is macOS only"),
]


async def test_live_capture_records_speech_and_frames(tmp_path: Path) -> None:
    started = datetime.now().astimezone()
    meeting = create_meeting(tmp_path, "live smoke", started_at=started)
    cfg = UserConfig(meetings_root=tmp_path, user=UserIdentity(name="Maria"))
    plan = LivePlan(meeting_dir=meeting, started_at=started, config=cfg, level=AutonomyLevel.L0)
    stop = StopSignal()

    async def stop_later() -> None:
        await asyncio.sleep(20)
        stop.request()

    stopper = asyncio.create_task(stop_later())
    await run_live(
        plan,
        stop,
        transcriber=build_transcriber(cfg.models.stt, vocabulary=["Maria"]),
        control_socket=None,  # the default path; pytest's tmp_path is too long for AF_UNIX on macOS
    )
    await stopper
    transcript = (meeting / "transcript.md").read_text(encoding="utf-8")
    sys.stdout.write(transcript)
    assert "TELA f0001" in transcript
    assert (meeting / "frames" / "f0001.jpg").is_file()
