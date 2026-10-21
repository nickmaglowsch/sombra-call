"""A live ``sombra start`` whose minutes call never returns (#83).

Run as a script by ``test_orchestrator_stuck_minutes.py``: ``python stuck_minutes.py
terminal|window <root>``. Capture runs on fakes; stderr says ``CAPTURING`` when the
sources open and ``MINUTES`` when the model is called for the minutes, which then
blocks forever, like an LLM call that hangs.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from fakes import FakeAudioSource, FakeScreenSource, FakeTranscriber
from sombra.config import UserConfig, UserIdentity
from sombra.contracts import AudioSource, AutonomyLevel, Channel, ScreenSource, SpeechLine, Usage
from sombra.orchestrator.live import LivePlan, run_in_terminal, run_with_window
from sombra.store import create_meeting

T0 = datetime(2026, 10, 21, 14, 0, 0).astimezone()
SCRIPT = [SpeechLine(T0 + timedelta(seconds=5), Channel.OTHERS, "a entrega fica para sexta")]


class HangingModel:
    """``TextModel`` fake whose ``complete()`` blocks forever."""

    name = "fake-haiku"

    def complete(self, system: str, user: str, max_tokens: int) -> tuple[str, Usage]:
        _say("MINUTES")
        threading.Event().wait()
        raise AssertionError("unreachable")


def _say(marker: str) -> None:
    sys.stderr.write(f"{marker}\n")
    sys.stderr.flush()


def sources(plan: LivePlan) -> tuple[AudioSource, ScreenSource]:
    _say("CAPTURING")
    return FakeAudioSource(interval_s=0.02, start=T0), FakeScreenSource()


def main(mode: str, root: Path) -> None:
    plan = LivePlan(
        meeting_dir=create_meeting(root, "Planejamento", started_at=T0),
        started_at=T0,
        config=UserConfig(meetings_root=root, user=UserIdentity(name="Maria")),
        level=AutonomyLevel.L0,
        summary=HangingModel(),
    )
    fakes = {
        "sources": sources,
        "transcriber": FakeTranscriber(SCRIPT),
        "control_socket": root / "c.sock",
    }
    if mode == "terminal":
        run_in_terminal(plan, **fakes)
    else:
        run_with_window(plan, "ctrl+alt+p", **fakes)


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
