"""Orchestrator: runs a meeting session by wiring the ports in ``sombra.contracts``.

Owns the event loop, the capture pipelines, autonomy levels L0/L1/L2, failure
handling (an agent failure never stops recording), latency timestamps and the
summary-epoch schedule (C4). PRD: "Fluxo do gatilho", "Confiabilidade".

This package is the only one allowed to import concrete modules. ``session`` and
``hooks`` depend on ``sombra.contracts`` alone and are tested against the fakes in
``tests/fakes/``; ``wiring``, ``live`` and ``replay`` build the real modules for
``sombra start`` and ``sombra replay`` (#17, see docs/usage.md). They are not
re-exported here, so importing the package stays cheap.
"""

from sombra.orchestrator.hooks import (
    BlockedApp,
    EpochHook,
    EpochSummary,
    FrameResolver,
    MinutesWriter,
    PauseState,
    PauseSwitch,
    Summarizer,
    never_blocked,
)
from sombra.orchestrator.session import Session, local_now
from sombra.orchestrator.settings import MAX_FRAMES_PER_CALL, SessionSettings
from sombra.orchestrator.supervisor import Supervised

__all__ = [
    "MAX_FRAMES_PER_CALL",
    "BlockedApp",
    "EpochHook",
    "EpochSummary",
    "FrameResolver",
    "MinutesWriter",
    "PauseState",
    "PauseSwitch",
    "Session",
    "SessionSettings",
    "Summarizer",
    "Supervised",
    "local_now",
    "never_blocked",
]
