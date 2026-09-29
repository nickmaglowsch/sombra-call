"""Device listing and config ids (A2), from PortAudio-style device dicts.

Ids are stable across reboots and replugs (unlike PortAudio indexes): the device name,
suffixed ``#2``, ``#3``... when several devices share a name. The system-audio process
tap has the fixed id :data:`TAP_ID`.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sombra.contracts import AudioDevice

TAP_ID = "tap:system"
TAP_NAME = "System audio (Core Audio process tap)"
TAP_MIN_MACOS = (14, 4)

# Virtual loopback drivers: they are inputs to PortAudio, but carry system output.
LOOPBACK_RE = re.compile(r"blackhole|loopback|soundflower", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class InputDevice:
    """A capturable device: its config id, PortAudio index and native format."""

    id: str
    index: int
    name: str
    channels: int
    sample_rate: int
    is_loopback: bool


def macos_version_tuple(release: str) -> tuple[int, ...]:
    """``"14.4.1"`` -> ``(14, 4, 1)``; ``""`` (not macOS) -> ``()``."""
    parts = []
    for p in release.split("."):
        if not p.isdigit():
            break
        parts.append(int(p))
    return tuple(parts)


def tap_supported(macos_release: str) -> bool:
    v = macos_version_tuple(macos_release)
    return bool(v) and v >= TAP_MIN_MACOS


def input_devices(raw: Sequence[Mapping[str, Any]]) -> list[InputDevice]:
    """Capturable devices from ``sounddevice.query_devices()`` output, with unique ids."""
    seen: Counter[str] = Counter()
    out = []
    for index, d in enumerate(raw):
        name = str(d["name"])
        seen[name] += 1
        if int(d.get("max_input_channels", 0)) <= 0:
            continue
        dev_id = name if seen[name] == 1 else f"{name}#{seen[name]}"
        out.append(
            InputDevice(
                id=dev_id,
                index=int(d.get("index", index)),
                name=name,
                channels=int(d["max_input_channels"]),
                sample_rate=round(float(d.get("default_samplerate", 48_000))),
                is_loopback=bool(LOOPBACK_RE.search(name)),
            )
        )
    return out


def list_audio_devices(
    raw: Sequence[Mapping[str, Any]], *, tap_available: bool
) -> list[AudioDevice]:
    """Microphones (``is_input=True``) first, then system-audio sources (tap, loopbacks)."""
    devs = input_devices(raw)
    mics = [AudioDevice(d.id, d.name, is_input=True) for d in devs if not d.is_loopback]
    system = [AudioDevice(TAP_ID, TAP_NAME, is_input=False)] if tap_available else []
    system += [AudioDevice(d.id, d.name, is_input=False) for d in devs if d.is_loopback]
    return mics + system


def find_device(raw: Sequence[Mapping[str, Any]], dev_id: str) -> InputDevice:
    for d in input_devices(raw):
        if d.id == dev_id:
            return d
    known = ", ".join(repr(d.id) for d in input_devices(raw)) or "none"
    raise LookupError(f"no audio input with id {dev_id!r} (known: {known})")


def find_loopback(raw: Sequence[Mapping[str, Any]]) -> InputDevice | None:
    return next((d for d in input_devices(raw) if d.is_loopback), None)


def default_input(raw: Sequence[Mapping[str, Any]], default_index: int) -> InputDevice:
    devs = [d for d in input_devices(raw) if not d.is_loopback]
    for d in devs:
        if d.index == default_index:
            return d
    if not devs:
        raise LookupError("no microphone found")
    return devs[0]
