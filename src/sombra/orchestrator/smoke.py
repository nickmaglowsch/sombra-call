"""One test question through the configured agent, for ``sombra setup`` and ``doctor --live``.

It runs the exact ``sombra ask`` path (:func:`.ask.ask_brain`, same flags, same login or
key) on a throwaway meeting folder with one synthetic transcript line, so a missing
login, an old CLI or a bad key shows up before a real meeting. The folder is removed
afterwards; nothing about the user's meetings is sent.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path

from sombra.config import UserConfig
from sombra.contracts import Brain, Channel, SpeechLine
from sombra.orchestrator.ask import Keys, ask, ask_brain
from sombra.orchestrator.session import local_now
from sombra.store import MeetingStore, create_meeting

SMOKE_LINE = "Reunião de teste do sombra setup: o prazo combinado é sexta-feira."


def smoke_meeting(root: Path, now: datetime) -> Path:
    """A synthetic meeting folder under ``root`` with one transcript line."""
    meeting = create_meeting(root, "sombra-setup-smoke", started_at=now)
    store = MeetingStore(meeting, started_at=now)
    try:
        store.append_entry(SpeechLine(ts=now, channel=Channel.OTHERS, text=SMOKE_LINE))
    finally:
        store.close()
    return meeting


async def smoke_async(brain: Brain, meeting: Path, question: str) -> str:
    result = await ask(brain, meeting, question, frames=False, clock=local_now)
    return result.response.text


def smoke_answer(cfg: UserConfig, question: str, keys: Keys | None = None) -> str:
    """Ask ``question`` through ``cfg``'s agent; returns the answer or raises."""
    if keys is None:  # pragma: no cover - the real keychain
        from sombra.orchestrator.commands import key_lookup

        keys = key_lookup
    with tempfile.TemporaryDirectory(prefix="sombra-smoke-") as tmp:
        meeting = smoke_meeting(Path(tmp), local_now())
        brain = ask_brain(meeting, cfg, frames=False, keys=keys)
        return asyncio.run(smoke_async(brain, meeting, question))
