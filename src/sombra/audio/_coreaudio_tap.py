"""Core Audio process tap (macOS 14.4+) exposed as a private aggregate input device.

A global stereo tap of every process's output is wrapped in a private aggregate device,
which PortAudio then opens like any microphone. That keeps all sample handling in one
place (:mod:`sombra.audio.capture`) instead of a second IOProc path.

Everything here needs a Mac and the "System Audio Recording" permission, so it runs only
in the ``hardware`` test (see ``docs/macos-audio.md``); CI covers the code around it with
a fake tap.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

TAP_DEVICE_PREFIX = "Sombra system audio"


def _coreaudio() -> Any:  # pragma: no cover - needs macOS
    # pyobjc-framework-CoreAudio ships no type stubs; keep it untyped and lazy.
    return importlib.import_module("CoreAudio")


@dataclass
class CoreAudioTap:
    tap_id: int
    aggregate_id: int
    device_name: str  # what PortAudio lists the aggregate device as

    def destroy(self) -> None:  # pragma: no cover - needs macOS 14.4+
        ca = _coreaudio()
        err = ca.AudioHardwareDestroyAggregateDevice(self.aggregate_id)
        if err:
            log.warning("AudioHardwareDestroyAggregateDevice failed: OSStatus %s", err)
        err = ca.AudioHardwareDestroyProcessTap(self.tap_id)
        if err:
            log.warning("AudioHardwareDestroyProcessTap failed: OSStatus %s", err)


def create_process_tap() -> CoreAudioTap:  # pragma: no cover - needs macOS 14.4+
    """Create a global, unmuted, private process tap and an aggregate device reading it."""
    import objc

    ca = _coreaudio()

    tap_description: Any = objc.lookUpClass("CATapDescription")
    desc = tap_description.alloc().initStereoGlobalTapButExcludeProcesses_([])
    uuid = str(desc.UUID().UUIDString())
    name = f"{TAP_DEVICE_PREFIX} {uuid[:8]}"
    desc.setName_(name)
    desc.setPrivate_(True)
    desc.setMuteBehavior_(0)  # CATapUnmuted: the user keeps hearing the call
    err, tap_id = ca.AudioHardwareCreateProcessTap(desc, None)
    if err:
        raise OSError(
            f"AudioHardwareCreateProcessTap failed (OSStatus {err}); "
            "grant System Audio Recording permission to your terminal"
        )
    aggregate = {
        "name": name,
        "uid": f"dev.sombra.systemtap.{uuid}",
        "private": True,
        "stacked": False,
        "tapautostart": True,
        "taps": [{"uid": uuid, "drift": True}],
    }
    err, aggregate_id = ca.AudioHardwareCreateAggregateDevice(aggregate, None)
    if err:
        ca.AudioHardwareDestroyProcessTap(tap_id)
        raise OSError(f"AudioHardwareCreateAggregateDevice failed (OSStatus {err})")
    log.info("created process tap %s -> aggregate device %r", tap_id, name)
    return CoreAudioTap(tap_id=int(tap_id), aggregate_id=int(aggregate_id), device_name=name)
