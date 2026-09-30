"""Fakes shared by the audio tests: a PortAudio-like ``sounddevice`` module and a tap.

Not a conftest: test directories have no ``__init__.py``, so a uniquely named helper
module avoids clashing with other packages' conftests.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable
from dataclasses import dataclass
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
        self.started = self.stopped = self.aborted = self.closed = False

    def start(self) -> None:
        if self.sd.fail_start_on == self.kwargs["device"]:
            raise OSError("device busy")
        self.started = True

    def stop(self) -> None:
        self._maybe_hang()
        self.stopped = True

    def abort(self) -> None:
        self._maybe_hang()
        self.aborted = self.stopped = True

    def _maybe_hang(self) -> None:
        """Like the Core Audio deadlock in #75: block until the test releases it."""
        if self.sd.hang_stop is not None:
            self.sd.hanging.append(self)
            self.sd.hang_stop.wait()

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


class FakeInputOutputPair:
    """Mirrors ``sounddevice._InputOutputPair``: ``__getitem__`` only, not a list or tuple.

    A ``None`` slot falls back to PortAudio's default (``FakeDefault.system_device``).
    """

    def __init__(self, pair: list[Any], fallback: tuple[int, int]) -> None:
        self._pair = pair
        self._fallback = fallback

    def __getitem__(self, index: int | str) -> Any:
        i = {"input": 0, "output": 1}.get(index, index) if isinstance(index, str) else index
        value = self._pair[i]
        return self._fallback[i] if value is None else value

    def __repr__(self) -> str:
        return f"[{self[0]!r}, {self[1]!r}]"


class FakeDefault:
    """Like ``sounddevice.default``: assigning ``device`` stores an input/output pair."""

    system_device: tuple[int, int] = (0, 1)

    def __init__(self) -> None:
        self.device = None

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "device" and not isinstance(value, FakeInputOutputPair):
            pair = list(value) if isinstance(value, list | tuple) else [value, value]
            value = FakeInputOutputPair(pair, self.system_device)
        object.__setattr__(self, name, value)


class FakeSounddevice:
    def __init__(self, devices: list[dict[str, Any]] | None = None) -> None:
        self.devices = [dict(d) for d in (devices if devices is not None else MAC_DEVICES)]
        self.default = FakeDefault()
        self.streams: list[FakeStream] = []
        self.reinitialised = 0
        self.after_reinit: list[dict[str, Any]] = []
        self.fail_start_on: int | None = None
        # Set to an unset Event to make stop()/abort() block until it is set.
        self.hang_stop: threading.Event | None = None
        self.hanging: list[FakeStream] = []

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


class HotplugSounddevice(FakeSounddevice):
    """PortAudio's view lags the hardware: ``hardware`` changes show up only after a re-init.

    Like Core Audio device ids, every plug gets a new ``uid``: a stale PortAudio entry
    for a device that was unplugged and plugged back in stays dead. :meth:`pump` feeds
    every open stream whose device is still there, like PortAudio's device threads.
    """

    def __init__(self, devices: list[dict[str, Any]] | None = None) -> None:
        super().__init__(devices)
        self._uids = itertools.count()
        for d in self.devices:
            d["uid"] = next(self._uids)
        self.hardware = [dict(d) for d in self.devices]
        self.fail_open: set[str] = set()

    def plug(self, name: str, inputs: int = 1, rate: float = 48000.0) -> None:
        self.hardware.append(
            {
                "name": name,
                "max_input_channels": inputs,
                "default_samplerate": rate,
                "uid": next(self._uids),
            }
        )

    def unplug(self, name: str) -> None:
        self.hardware = [d for d in self.hardware if d["name"] != name]

    def _initialize(self) -> None:
        self.reinitialised += 1
        for d in self.after_reinit:
            self.hardware.append(dict(d, uid=next(self._uids)))
        self.after_reinit = []
        self.devices = [dict(d) for d in self.hardware]

    def InputStream(self, **kwargs: Any) -> FakeStream:
        dev = self.devices[kwargs["device"]]
        if dev["name"] in self.fail_open:
            raise OSError(f"cannot open {dev['name']}")
        s = super().InputStream(**kwargs)
        s.uid = dev["uid"]  # type: ignore[attr-defined]
        return s

    def open_streams(self) -> list[FakeStream]:
        return [s for s in self.streams if s.started and not s.stopped]

    def pump(self, block_ms: int = 10) -> None:
        present = {d["uid"] for d in self.hardware}
        for s in self.open_streams():
            if s.uid in present:  # type: ignore[attr-defined]
                n = s.kwargs["samplerate"] * block_ms // 1000
                s.callback(np.zeros((n, s.kwargs["channels"]), np.float32), n, None, Flags())


class FakeWatcher:
    """Core Audio's live view of a :class:`HotplugSounddevice`."""

    def __init__(self, sd: HotplugSounddevice, default_input: str | None = None) -> None:
        self.sd = sd
        self.default_input = default_input
        self.default_output: str | None = "MacBook Pro Speakers"
        self.on_change: Callable[[], None] | None = None
        self.stopped = False

    def start(self, on_change: Callable[[], None]) -> None:
        self.on_change = on_change

    def stop(self) -> None:
        self.stopped = True

    def changed(self) -> None:
        assert self.on_change is not None
        self.on_change()

    def input_names(self) -> set[str]:
        return {d["name"] for d in self.sd.hardware if d.get("max_input_channels", 0) > 0}

    def default_input_name(self) -> str | None:
        return self.default_input

    def default_output_name(self) -> str | None:
        return self.default_output
