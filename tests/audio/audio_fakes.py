"""Fakes shared by the audio tests: a PortAudio-like ``sounddevice`` module and a tap.

Not a conftest: test directories have no ``__init__.py``, so a uniquely named helper
module avoids clashing with other packages' conftests.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

MAC_DEVICES: list[dict[str, Any]] = [
    {
        "name": "MacBook Pro Microphone",
        "max_input_channels": 1,
        "max_output_channels": 0,
        "default_samplerate": 48000.0,
    },
    {
        "name": "MacBook Pro Speakers",
        "max_input_channels": 0,
        "max_output_channels": 2,
        "default_samplerate": 48000.0,
    },
    {
        "name": "BlackHole 2ch",
        "max_input_channels": 2,
        "max_output_channels": 2,
        "default_samplerate": 48000.0,
    },
    {
        "name": "USB Mic",
        "max_input_channels": 2,
        "max_output_channels": 0,
        "default_samplerate": 44100.0,
    },
    {
        "name": "USB Mic",
        "max_input_channels": 1,
        "max_output_channels": 0,
        "default_samplerate": 16000.0,
    },
]


@dataclass
class Flags:
    input_overflow: bool = False


class FakeStream:
    def __init__(self, sd: FakeSounddevice, **kwargs: Any) -> None:
        self.sd = sd
        self.kwargs = kwargs
        self.callback: Callable[..., None] = kwargs["callback"]
        self.started = self.stopped = self.closed = False

    def start(self) -> None:
        if self.sd.fail_start_on == self.kwargs["device"]:
            raise OSError("device busy")
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def feed(self, seconds: float, value: float = 0.1, *, block_ms: int = 10) -> None:
        """Drive the callback from another thread, like PortAudio does."""
        rate = self.kwargs["samplerate"]
        n = rate * block_ms // 1000
        block = np.full((n, self.kwargs["channels"]), value, dtype=np.float32)

        def run() -> None:
            for i in range(int(seconds * 1000 / block_ms)):
                self.callback(block, n, None, Flags(input_overflow=(i == 0)))

        t = threading.Thread(target=run)
        t.start()
        t.join()


@dataclass
class FakeDefault:
    device: Any = field(default_factory=lambda: [0, 1])


class FakeSounddevice:
    def __init__(self, devices: list[dict[str, Any]] | None = None) -> None:
        self.devices = [dict(d) for d in (devices if devices is not None else MAC_DEVICES)]
        self.default = FakeDefault()
        self.streams: list[FakeStream] = []
        self.reinitialised = 0
        self.after_reinit: list[dict[str, Any]] = []
        self.fail_start_on: int | None = None

    def query_devices(self) -> list[dict[str, Any]]:
        return [dict(d, index=i) for i, d in enumerate(self.devices)]

    def _terminate(self) -> None:
        pass

    def _initialize(self) -> None:
        self.reinitialised += 1
        self.devices += self.after_reinit
        self.after_reinit = []

    def InputStream(self, **kwargs: Any) -> FakeStream:
        s = FakeStream(self, **kwargs)
        self.streams.append(s)
        return s


class FakeTap:
    def __init__(self, sd: FakeSounddevice, name: str = "Sombra system audio 1234abcd") -> None:
        self.device_name = name
        self.destroyed = False
        sd.after_reinit.append(
            {
                "name": name,
                "max_input_channels": 2,
                "max_output_channels": 0,
                "default_samplerate": 48000.0,
            }
        )

    def destroy(self) -> None:
        self.destroyed = True
