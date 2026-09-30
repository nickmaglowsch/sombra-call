"""A ``MacAudioSource`` over a PortAudio fake whose ``stop``/``abort`` never returns.

Reproduces #75 (a Core Audio deadlock in PortAudio's stop) without a Mac. Shared by the
in-process test and the ``sombra start`` subprocess in
``test_orchestrator_stuck_audio.py``.
"""

from __future__ import annotations

import ctypes
import json
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from sombra.audio.macos import MacAudioSource
from sombra.config import UserConfig, UserIdentity
from sombra.contracts import AutonomyLevel, Channel, SpeechLine, Usage
from sombra.orchestrator.live import LivePlan
from sombra.store import create_meeting

T0 = datetime(2026, 9, 30, 14, 0, 0).astimezone()
MINUTES = {"resumo": "Prazo da entrega definido.", "decisoes": ["entrega na sexta"], "acoes": []}
SCRIPT = [SpeechLine(T0 + timedelta(seconds=5), Channel.OTHERS, "a entrega fica para sexta")]


class StuckStream:
    def __init__(self, sd: StuckPortAudio, kwargs: dict[str, Any]) -> None:
        self.sd = sd
        self.kwargs = kwargs
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self._hang()

    def abort(self) -> None:
        self._hang()

    def close(self) -> None:
        pass

    def _hang(self) -> None:
        if self.sd.announce:
            sys.stderr.write("STUCK\n")
            sys.stderr.flush()
        self.sd.stuck.set()
        self.sd.block_in_c()


_LIBC = ctypes.CDLL(None)


class CMutex:
    """A pthread mutex, locked by its creator. Waiting on it blocks in C, where no
    Python signal handler runs, like the ``__psynch_mutexwait`` in #75 (an
    ``Event.wait`` would let Ctrl+C through and prove nothing)."""

    def __init__(self) -> None:
        self._mutex = ctypes.create_string_buffer(256)  # >= sizeof(pthread_mutex_t)
        _LIBC.pthread_mutex_init(self._mutex, None)
        _LIBC.pthread_mutex_lock(self._mutex)
        self._released = False

    def wait(self) -> None:
        _LIBC.pthread_mutex_lock(self._mutex)  # releases the GIL; ignores signals
        _LIBC.pthread_mutex_unlock(self._mutex)

    def release(self) -> None:
        """Call from the creating thread (the mutex's owner)."""
        if not self._released:
            self._released = True
            _LIBC.pthread_mutex_unlock(self._mutex)


class StuckPortAudio:
    """Enough of ``sounddevice`` for one mic and BlackHole."""

    def __init__(self, *, announce: bool = False) -> None:
        self.announce = announce
        self.default = SimpleNamespace(device=(0, 1))
        self.streams: list[StuckStream] = []
        self.stuck = threading.Event()
        self._mutex = CMutex()

    def block_in_c(self) -> None:
        self._mutex.wait()

    def release(self) -> None:
        self._mutex.release()

    def query_devices(self) -> list[dict[str, Any]]:
        devices = [("MacBook Pro Microphone", 1), ("BlackHole 2ch", 2)]
        return [
            {
                "name": name,
                "index": i,
                "max_input_channels": channels,
                "max_output_channels": 0,
                "default_samplerate": 48000.0,
            }
            for i, (name, channels) in enumerate(devices)
        ]

    def InputStream(self, **kwargs: Any) -> StuckStream:
        stream = StuckStream(self, kwargs)
        self.streams.append(stream)
        return stream

    def speak(self, seconds: float, block_ms: int = 10) -> None:
        """Drive every stream's callback, like PortAudio's device threads."""
        for s in self.streams:
            n = int(s.kwargs["samplerate"]) * block_ms // 1000
            block = np.full((n, s.kwargs["channels"]), 0.1, dtype=np.float32)
            for _ in range(int(seconds * 1000 / block_ms)):
                s.kwargs["callback"](block, n, None, None)


def stuck_source(sd: StuckPortAudio, close_timeout_s: float) -> MacAudioSource:
    return MacAudioSource(
        system="BlackHole 2ch",
        sounddevice=sd,
        macos_release="14.5",
        reconnect=False,
        close_timeout_s=close_timeout_s,
    )


class MinutesModel:
    """``TextModel`` fake: every call answers with the same minutes JSON."""

    name = "fake-haiku"

    def complete(self, system: str, user: str, max_tokens: int) -> tuple[str, Usage]:
        return json.dumps(MINUTES), Usage(input_tokens=10, output_tokens=5)


def live_plan(root: Path, *, summary: bool = False) -> LivePlan:
    return LivePlan(
        meeting_dir=create_meeting(root, "Planejamento", started_at=T0),
        started_at=T0,
        config=UserConfig(meetings_root=root, user=UserIdentity(name="Maria")),
        level=AutonomyLevel.L0,
        summary=MinutesModel() if summary else None,  # type: ignore[arg-type]
    )
