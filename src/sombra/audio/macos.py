"""macOS capture: microphone -> ``Channel.ME``, system output -> ``Channel.OTHERS`` (A1, A2).

System audio comes from a Core Audio process tap (macOS 14.4+) or, as a fallback, a
user-installed loopback driver such as BlackHole. :attr:`MacAudioSource.system_path`
records which one is in use. Both paths, and the mic, are opened through PortAudio
(``sounddevice``), imported lazily so this module imports on any OS.

Reconnect (A3): a :class:`~sombra.audio.reconnect.ReconnectSupervisor` restarts a
channel whose device disappears (Core Audio notification) or goes quiet (stall
watchdog), re-resolving the mic by config id -> name -> system default, and reports
every transition through ``on_status``. PortAudio enumerates devices only when it is
initialised, and re-initialising it closes every open stream. So a channel is reopened
on its own whenever its replacement device is already enumerated (a headset unplugged:
fall back to the built-in mic; the tap's aggregate device after an output switch). Only
when a device must be (re)discovered (a headset plugged back in, a new process tap) are
both channels reopened, and the status events show the other channel's short gap too.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import platform
import re
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Protocol

import numpy as np

from sombra.audio._coreaudio_tap import TAP_DEVICE_PREFIX
from sombra.audio.buffer import DropOldestQueue
from sombra.audio.capture import ChannelCapture
from sombra.audio.clock import MonotonicClock
from sombra.audio.devices import (
    LOOPBACK_RE,
    TAP_ID,
    InputDevice,
    default_input,
    find_device,
    find_loopback,
    input_devices,
    list_audio_devices,
    tap_supported,
)
from sombra.audio.reconnect import Backoff, ReconnectSupervisor, StatusHook
from sombra.contracts import AudioChunk, AudioDevice, Channel

log = logging.getLogger(__name__)

AUTO = "auto"
PROCESS_TAP = "process-tap"
#: Every this-many failed attempts on a channel, re-enumerate devices even if the old
#: enumeration still offers a candidate (it may be stale).
REENUMERATE_EVERY = 3

_ID_SUFFIX = re.compile(r"^(.*)#\d+$")


class ProcessTap(Protocol):
    device_name: str

    def destroy(self) -> None: ...


class DeviceWatcher(Protocol):
    """Live device state from the OS, independent of PortAudio's one-off enumeration."""

    def start(self, on_change: Callable[[], None]) -> None: ...

    def stop(self) -> None: ...

    def input_names(self) -> set[str]: ...

    def default_input_name(self) -> str | None: ...

    def default_output_name(self) -> str | None: ...


def _load_sounddevice() -> Any:  # pragma: no cover - needs PortAudio (macOS wheel)
    return importlib.import_module("sounddevice")


def _create_tap() -> ProcessTap:  # pragma: no cover - needs macOS 14.4+
    from sombra.audio._coreaudio_tap import create_process_tap

    return create_process_tap()


def _create_watcher() -> DeviceWatcher:  # pragma: no cover - needs macOS
    from sombra.audio._coreaudio_watch import create_watcher

    return create_watcher()


class MacAudioSource:
    """``contracts.AudioSource`` for macOS.

    Args:
        mic: device id from ``sombra devices`` for the microphone; ``None`` = system default.
        system: ``"auto"`` (process tap on 14.4+, else the first loopback device),
            ``"tap:system"`` (process tap only), or a loopback device id such as
            ``"BlackHole 2ch"``.
        chunk_ms: chunk length, 20-100 ms.
        max_buffer_s: audio kept per channel when the consumer stalls; older chunks drop.
        on_status: called with a :class:`~sombra.audio.reconnect.SourceStatus` when a
            channel is lost, a restart attempt fails, or it is restored. Runs on the
            reconnect thread: keep it quick and thread-safe.
        reconnect: supervise the channels and restart lost ones (A3).
        stall_timeout_s: a channel silent at the device level (no callbacks) this long
            is lost.
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
        on_status: StatusHook | None = None,
        reconnect: bool = True,
        watcher_factory: Callable[[], DeviceWatcher] | None = _create_watcher,
        stall_timeout_s: float = 0.5,
        confirm_timeout_s: float = 1.0,
        backoff: Backoff | None = None,
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
        self._on_status = on_status
        self._reconnect = reconnect
        self._watcher_factory = watcher_factory
        self._stall_timeout_s = stall_timeout_s
        self._confirm_timeout_s = confirm_timeout_s
        self._backoff = backoff
        # Guards streams, devices and the tap: the reconnect thread reopens them.
        self._lock = threading.RLock()
        self._streams: dict[Channel, Any] = {}
        self._captures: dict[Channel, ChannelCapture] = {}
        self._devices: dict[Channel, InputDevice] = {}
        self._generation: dict[Channel, int] = {}
        self._closed_overflows = 0
        # Last stamp handed out per channel by a closed stream: the next one continues after it.
        self._stamp_floor: dict[Channel, int] = {}
        self._supervisor: ReconnectSupervisor | None = None
        self._watcher: DeviceWatcher | None = None
        self._output_name: str | None = None
        # Devices seen gone since PortAudio last enumerated: their entries are dead even
        # if a device with the same name comes back (it gets a new Core Audio id).
        self._stale_names: set[str] = set()
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
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:  # waits for a restart in progress
            await asyncio.to_thread(supervisor.stop)
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            try:
                watcher.stop()
            except Exception:
                log.exception("error stopping the audio device watcher")
        with self._lock:
            for channel in list(self._streams):
                self._close_channel(channel)
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
        # No lock: the reconnect thread holds it through a PortAudio re-init, and this may
        # be read from the event loop. tuple() snapshots the dict atomically under the GIL.
        return self._closed_overflows + sum(c.overflows for c in tuple(self._captures.values()))

    # --- internals ---------------------------------------------------------------------

    def _sounddevice(self) -> Any:
        if self._sd is None:
            self._sd = _load_sounddevice()
        return self._sd

    def start(self) -> None:
        """Open both streams. Called by :meth:`stream`; exposed for tests and tools."""
        with self._lock:
            if self._started:
                return
            self._started = True
            with contextlib.suppress(RuntimeError):  # no running loop: caller binds the queue
                self._queue.bind(asyncio.get_running_loop())
            sd = self._sounddevice()
            # System first: the tap path re-initialises PortAudio, which can renumber every
            # device, so the mic is resolved (by id) only from the final enumeration.
            system = self._resolve_system(sd)
            try:
                raw = sd.query_devices()
                mic = (
                    find_device(raw, self.mic) if self.mic else default_input(raw, _default_in(sd))
                )
                log.info("audio: mic=%r system=%r via %s", mic.id, system.id, self.system_path)
                self.mic_id = mic.id
                self._open(sd, mic, Channel.ME)
                self._open(sd, system, Channel.OTHERS)
            except Exception:
                for channel in list(self._streams):
                    self._close_channel(channel)
                if self._tap is not None:
                    self._tap.destroy()
                    self._tap = None
                raise
        if self._reconnect:
            self._supervise()

    # --- reconnect (A3) ----------------------------------------------------------------

    def _supervise(self) -> None:
        self._supervisor = ReconnectSupervisor(
            self._restart_channel,
            check=self._check_devices,
            on_status=self._on_status,
            clock=self._clock,
            backoff=self._backoff,
            stall_timeout_s=self._stall_timeout_s,
            confirm_timeout_s=self._confirm_timeout_s,
        )
        if self._watcher_factory is not None:
            try:
                watcher = self._watcher_factory()
                mic = self._devices[Channel.ME]
                if mic.name not in watcher.input_names():
                    # Matching by name would then flag the mic as lost on every change.
                    raise LookupError(f"Core Audio does not list the open mic {mic.name!r}")
                self._output_name = watcher.default_output_name()
                watcher.start(self._supervisor.poke)
                self._watcher = watcher
            except Exception as e:
                log.warning(
                    "audio: no device-change notifications (%s); relying on the stall watchdog",
                    e,
                )
        self._supervisor.start()

    def _live_inputs(self, sd: Any) -> tuple[set[str] | None, str | None]:
        """Openable input names now and the default input's name (``None``: unknown).

        A name is openable if the device is connected and PortAudio's entry for it is
        not stale.
        """
        if self._watcher is None:
            return None, None
        live = self._watcher.input_names()
        self._stale_names |= self._enumerated(sd) - live
        return live - self._stale_names, self._watcher.default_input_name()

    def _preferred_mic(self, live: set[str] | None, default_name: str | None) -> str | None:
        """Name of the mic we would rather use, if it is connected now."""
        if live is None:
            return None
        if self.mic:
            name = _name_of(self.mic)
            return name if name in live else None
        if default_name is not None and default_name in live and _is_mic_name(default_name):
            return default_name
        return None

    def _check_devices(self) -> list[tuple[Channel, str]]:
        """Supervisor thread, after a Core Audio notification: which channels to restart."""
        watcher = self._watcher
        if watcher is None:
            return []
        lost: list[tuple[Channel, str]] = []
        with self._lock:
            live, default_name = watcher.input_names(), watcher.default_input_name()
            self._stale_names |= self._enumerated(self._sounddevice()) - live
            mic = self._devices.get(Channel.ME)
        if mic is not None:
            preferred = self._preferred_mic(live, default_name)
            if mic.name not in live or mic.name in self._stale_names:
                lost.append((Channel.ME, f"microphone {mic.name!r} disconnected"))
            elif preferred is not None and preferred != mic.name:
                lost.append((Channel.ME, f"switching to microphone {preferred!r}"))
        output = watcher.default_output_name()
        if output != self._output_name:
            before, self._output_name = self._output_name, output
            # A loopback driver does not care where the output goes; the tap may.
            if self.system_path == PROCESS_TAP:
                lost.append((Channel.OTHERS, f"default output changed {before!r} -> {output!r}"))
        return lost

    def _restart_channel(self, channel: Channel, attempt: int) -> Mapping[Channel, str]:
        """Supervisor thread: reopen ``channel``; see :data:`reconnect.RestartFn`."""
        with self._lock:
            if self._closed:
                raise RuntimeError("audio source is closed")
            sd = self._sounddevice()
            self._close_channel(channel)
            escalate = attempt % REENUMERATE_EVERY == 0
            if channel is Channel.ME:
                live, default_name = self._live_inputs(sd)
                mics = self._mic_candidates(sd, live, default_name)
                connected = self._watcher.input_names() if self._watcher is not None else None
                preferred = self._preferred_mic(connected, default_name)
                unseen = (connected or set()) - (self._enumerated(sd) - self._stale_names)
                if not mics and connected is not None and not any(map(_is_mic_name, unseen)):
                    raise LookupError("no microphone connected")
                if not mics or escalate or (preferred is not None and mics[0].name != preferred):
                    # The device we want is connected but PortAudio has not seen it yet.
                    return self._reopen_all(sd, channel, new_tap=False)
                mic = self._open_first(sd, mics, Channel.ME)
                self.mic_id = mic.id
                return {Channel.ME: mic.id}
            if self.system_path == PROCESS_TAP:
                if attempt > 1 or self._tap is None:  # reopening the aggregate did not help
                    return self._reopen_all(sd, channel, new_tap=True)
                name = self._tap.device_name
            else:
                name = (self.system_path or "").removeprefix("loopback:")
            try:
                dev = find_device(sd.query_devices(), name)
            except LookupError:
                return self._reopen_all(sd, channel, new_tap=False)
            if escalate:
                return self._reopen_all(sd, channel, new_tap=False)
            self._open(sd, dev, Channel.OTHERS)
            return {Channel.OTHERS: dev.id}

    def _reopen_all(self, sd: Any, lost: Channel, *, new_tap: bool) -> dict[Channel, str]:
        """Re-initialise PortAudio (closes every stream) and reopen both channels.

        Each channel is reopened on its own: if only the other one fails, it is handed
        back to the supervisor as lost, and ``lost`` still counts as restarted.
        """
        for channel in list(self._streams):
            self._close_channel(channel)
        opened: dict[Channel, str] = {}
        errors: dict[Channel, Exception] = {}
        system: InputDevice | None = None
        try:
            if self._tap is not None and new_tap:
                self._tap.destroy()
                self._tap = None
            if self._tap is not None:
                self._reinitialise(sd)
                system = find_device(sd.query_devices(), self._tap.device_name)
            else:
                system = self._resolve_system(sd, reinit=True)
        except Exception as e:
            errors[Channel.OTHERS] = e
        try:
            live, default_name = self._live_inputs(sd)
            mic = self._open_first(sd, self._mic_candidates(sd, live, default_name), Channel.ME)
            self.mic_id = opened[Channel.ME] = mic.id
        except Exception as e:
            errors[Channel.ME] = e
        if system is not None:
            try:
                self._open(sd, system, Channel.OTHERS)
                opened[Channel.OTHERS] = system.id
            except Exception as e:
                errors[Channel.OTHERS] = e
        log.info("audio: re-enumerated devices: opened %s, failed %s", opened, errors)
        for channel, error in errors.items():
            if channel is not lost and self._supervisor is not None:
                self._supervisor.report_lost(channel, f"reopen after re-enumeration: {error}")
        if lost in errors:
            raise errors[lost]
        return opened

    def _open_first(self, sd: Any, candidates: list[InputDevice], channel: Channel) -> InputDevice:
        """Open the first candidate that works."""
        error: Exception = LookupError("no microphone connected")
        for dev in candidates:
            try:
                self._open(sd, dev, channel)
            except Exception as e:
                log.warning("audio: cannot open %r for %s: %s", dev.id, channel.value, e)
                self._close_channel(channel)
                error = e
            else:
                return dev
        raise error

    def _mic_candidates(
        self, sd: Any, live: set[str] | None, default_name: str | None
    ) -> list[InputDevice]:
        """Connected mics, best first: config id -> same name -> system default -> others."""
        mics = [
            d
            for d in input_devices(sd.query_devices())
            if not d.is_loopback and _is_mic_name(d.name) and (live is None or d.name in live)
        ]
        default_index = _default_in(sd)

        def rank(d: InputDevice) -> int:
            if self.mic and d.id == self.mic:
                return 0
            if self.mic and d.name == _name_of(self.mic):
                return 1
            if default_name is not None and d.name == default_name:
                return 2
            if default_name is None and d.index == default_index:
                return 3
            return 4

        return sorted(mics, key=rank)  # stable: PortAudio order among equals

    def _enumerated(self, sd: Any) -> set[str]:
        return {d.name for d in input_devices(sd.query_devices())}

    def _resolve_system(self, sd: Any, *, reinit: bool = False) -> InputDevice:
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
        if reinit:
            self._reinitialise(sd)
        raw = sd.query_devices()
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
            self._reinitialise(sd)
            dev = find_device(sd.query_devices(), tap.device_name)
        except Exception:
            tap.destroy()
            raise
        self._tap = tap
        self.system_path = PROCESS_TAP
        return dev

    def _open(self, sd: Any, dev: InputDevice, channel: Channel) -> None:
        cap = ChannelCapture(
            channel,
            dev.sample_rate,
            self._clock,
            self._queue,
            chunk_ms=self.chunk_ms,
            floor_ns=self._stamp_floor.get(channel),
        )
        channels = min(dev.channels, 2)
        generation = self._generation[channel] = self._generation.get(channel, 0) + 1

        def callback(
            indata: np.ndarray[Any, Any], frames: int, time_info: Any, status: Any
        ) -> None:
            if self._generation.get(channel) != generation:
                return  # a stream we already replaced
            try:
                cap.on_frames(indata, overflow=bool(getattr(status, "input_overflow", False)))
            except Exception:  # never let an exception kill the device callback
                log.exception("audio callback failed on %s", channel.value)
            supervisor = self._supervisor
            if supervisor is not None:
                supervisor.frame(channel)

        stream = sd.InputStream(
            device=dev.index,
            channels=channels,
            samplerate=dev.sample_rate,
            dtype="float32",
            latency="low",
            callback=callback,
        )
        self._streams[channel] = stream
        self._captures[channel] = cap
        self._devices[channel] = dev
        stream.start()

    def _reinitialise(self, sd: Any) -> None:
        """Make PortAudio enumerate devices again. Closes every open stream."""
        sd._terminate()
        sd._initialize()
        self._stale_names.clear()

    def _close_channel(self, channel: Channel) -> None:
        self._generation[channel] = self._generation.get(channel, 0) + 1
        self._devices.pop(channel, None)
        stream = self._streams.pop(channel, None)
        if stream is not None:
            try:
                stream.stop()  # waits for a callback in flight
                stream.close()
            except Exception:  # a vanished device may fail to stop; keep going
                log.exception("error closing audio stream on %s", channel.value)
        cap = self._captures.pop(channel, None)
        if cap is not None:
            self._closed_overflows += cap.overflows
            if cap.timer.last_stamp_ns is not None:
                self._stamp_floor[channel] = cap.timer.last_stamp_ns
        if self._supervisor is not None:
            # The old stream is stopped: only audio from the next one may confirm a restart.
            self._supervisor.forget_frames(channel)


def _is_mic_name(name: str) -> bool:
    """Not a loopback driver and not our own system-audio tap."""
    return not LOOPBACK_RE.search(name) and not name.startswith(TAP_DEVICE_PREFIX)


def _name_of(dev_id: str) -> str:
    """Config id -> device name: ``"USB Mic#2"`` -> ``"USB Mic"``."""
    m = _ID_SUFFIX.match(dev_id)
    return m.group(1) if m else dev_id


def _default_in(sd: Any) -> int:
    dev = sd.default.device
    idx = dev[0] if isinstance(dev, list | tuple) else dev
    return int(idx) if idx is not None else -1
