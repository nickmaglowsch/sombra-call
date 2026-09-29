"""macOS capture: microphone -> ``Channel.ME``, system output -> ``Channel.OTHERS`` (A1, A2).

System audio comes from a Core Audio process tap (macOS 14.4+) or, as a fallback, a
user-installed loopback driver such as BlackHole. :attr:`MacAudioSource.system_path`
records which one is in use. Both paths, and the mic, are opened through PortAudio
(``sounddevice``), imported lazily so this module imports on any OS.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import platform
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any, Protocol

import numpy as np

from sombra.audio.buffer import DropOldestQueue
from sombra.audio.capture import ChannelCapture
from sombra.audio.clock import MonotonicClock
from sombra.audio.devices import (
    TAP_ID,
    InputDevice,
    default_input,
    find_device,
    find_loopback,
    list_audio_devices,
    tap_supported,
)
from sombra.contracts import AudioChunk, AudioDevice, Channel

log = logging.getLogger(__name__)

AUTO = "auto"


class ProcessTap(Protocol):
    device_name: str

    def destroy(self) -> None: ...


def _load_sounddevice() -> Any:  # pragma: no cover - needs PortAudio (macOS wheel)
    return importlib.import_module("sounddevice")


def _create_tap() -> ProcessTap:  # pragma: no cover - needs macOS 14.4+
    from sombra.audio._coreaudio_tap import create_process_tap

    return create_process_tap()


class MacAudioSource:
    """``contracts.AudioSource`` for macOS.

    Args:
        mic: device id from ``sombra devices`` for the microphone; ``None`` = system default.
        system: ``"auto"`` (process tap on 14.4+, else the first loopback device),
            ``"tap:system"`` (process tap only), or a loopback device id such as
            ``"BlackHole 2ch"``.
        chunk_ms: chunk length, 20-100 ms.
        max_buffer_s: audio kept per channel when the consumer stalls; older chunks drop.
    """

    def __init__(
        self,
        *,
        mic: str | None = None,
        system: str = AUTO,
        chunk_ms: int = 50,
        max_buffer_s: float = 10.0,
        clock: MonotonicClock | None = None,
        sounddevice: Any = None,
        tap_factory: Callable[[], ProcessTap] = _create_tap,
        macos_release: str | None = None,
    ) -> None:
        self.mic = mic
        self.system = system
        self.chunk_ms = chunk_ms
        self._clock = clock or MonotonicClock()
        self._sd = sounddevice
        self._tap_factory = tap_factory
        self._macos_release = platform.mac_ver()[0] if macos_release is None else macos_release
        maxlen = max(1, round(2 * max_buffer_s * 1000 / chunk_ms))
        self._queue = DropOldestQueue(maxlen)
        self._streams: list[Any] = []
        self._captures: list[ChannelCapture] = []
        self._tap: ProcessTap | None = None
        self._started = False
        self._closed = False
        self.system_path: str | None = None  # "process-tap" or "loopback:<id>"
        self.mic_id: str | None = None

    # --- AudioSource -------------------------------------------------------------------

    def list_devices(self) -> list[AudioDevice]:
        sd = self._sounddevice()
        return list_audio_devices(
            sd.query_devices(), tap_available=tap_supported(self._macos_release)
        )

    async def stream(self) -> AsyncIterator[AudioChunk]:
        self._queue.bind(asyncio.get_running_loop())
        self.start()
        try:
            while (chunk := await self._queue.get()) is not None:
                yield chunk
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for s in self._streams:
            try:
                s.stop()
                s.close()
            except Exception:  # keep closing the rest
                log.exception("error closing audio stream")
        self._streams.clear()
        if self._tap is not None:
            self._tap.destroy()
            self._tap = None
        self._queue.close()
        if self.dropped_chunks:
            log.warning("audio: dropped %s chunks (consumer too slow)", dict(self._queue.dropped))

    # --- stats -------------------------------------------------------------------------

    @property
    def dropped_chunks(self) -> int:
        return self._queue.dropped_total

    @property
    def overflows(self) -> int:
        return sum(c.overflows for c in self._captures)

    # --- internals ---------------------------------------------------------------------

    def _sounddevice(self) -> Any:
        if self._sd is None:
            self._sd = _load_sounddevice()
        return self._sd

    def start(self) -> None:
        """Open both streams. Called by :meth:`stream`; exposed for tests and tools."""
        if self._started:
            return
        self._started = True
        sd = self._sounddevice()
        raw = sd.query_devices()
        mic = find_device(raw, self.mic) if self.mic else default_input(raw, _default_in(sd))
        system = self._resolve_system(sd, raw)
        log.info("audio: mic=%r system=%r via %s", mic.id, system.id, self.system_path)
        self.mic_id = mic.id
        try:
            self._open(sd, mic, Channel.ME)
            self._open(sd, system, Channel.OTHERS)
        except Exception:
            for s in self._streams:
                s.stop()
                s.close()
            self._streams.clear()
            if self._tap is not None:
                self._tap.destroy()
                self._tap = None
            raise

    def _resolve_system(self, sd: Any, raw: Sequence[Mapping[str, Any]]) -> InputDevice:
        want_tap = self.system == TAP_ID or (
            self.system == AUTO and tap_supported(self._macos_release)
        )
        if want_tap:
            try:
                return self._open_tap(sd)
            except Exception as e:
                if self.system == TAP_ID:
                    raise
                log.warning("audio: process tap unavailable (%s); trying a loopback device", e)
        if self.system in (AUTO, TAP_ID):
            dev = find_loopback(raw)
            if dev is None:
                raise LookupError(
                    "no system-audio source: process taps need macOS 14.4+ and the System "
                    "Audio Recording permission; otherwise install BlackHole "
                    "(see docs/macos-audio.md)"
                )
        else:
            dev = find_device(raw, self.system)
        self.system_path = f"loopback:{dev.id}"
        return dev

    def _open_tap(self, sd: Any) -> InputDevice:
        tap = self._tap_factory()
        try:
            # PortAudio enumerates devices once; re-initialise it to see the new aggregate.
            sd._terminate()
            sd._initialize()
            dev = find_device(sd.query_devices(), tap.device_name)
        except Exception:
            tap.destroy()
            raise
        self._tap = tap
        self.system_path = "process-tap"
        return dev

    def _open(self, sd: Any, dev: InputDevice, channel: Channel) -> None:
        cap = ChannelCapture(
            channel, dev.sample_rate, self._clock, self._queue, chunk_ms=self.chunk_ms
        )
        channels = min(dev.channels, 2)

        def callback(
            indata: np.ndarray[Any, Any], frames: int, time_info: Any, status: Any
        ) -> None:
            try:
                cap.on_frames(indata, overflow=bool(getattr(status, "input_overflow", False)))
            except Exception:  # never let an exception kill the device callback
                log.exception("audio callback failed on %s", channel.value)

        stream = sd.InputStream(
            device=dev.index,
            channels=channels,
            samplerate=dev.sample_rate,
            dtype="float32",
            latency="low",
            callback=callback,
        )
        self._streams.append(stream)
        self._captures.append(cap)
        stream.start()


def _default_in(sd: Any) -> int:
    dev = sd.default.device
    idx = dev[0] if isinstance(dev, list | tuple) else dev
    return int(idx) if idx is not None else -1
