"""Core Audio device-change notifications and live device names (macOS, PRD A3).

PortAudio enumerates devices once, so it cannot tell that a headset went away or came
back. This watcher asks Core Audio directly: it registers property listeners on the
system object for the device list and the default input/output, and answers "which
inputs exist right now" and "what is the default input/output".

It uses ``ctypes`` against the CoreAudio and CoreFoundation frameworks rather than
pyobjc, because listener procs and variable-size property data are plain C. Nothing
is loaded until :func:`create_watcher` runs, so this module imports on any OS; the
listener path itself runs only in the ``hardware`` test (see ``docs/macos-audio.md``).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


def fourcc(code: str) -> int:
    """``"dev#"`` -> the ``UInt32`` Core Audio selector."""
    raw = code.encode("ascii")
    if len(raw) != 4:
        raise ValueError(f"four-char code must be 4 characters: {code!r}")
    return int.from_bytes(raw, "big")


SYSTEM_OBJECT = 1  # kAudioObjectSystemObject
ELEMENT_MAIN = 0  # kAudioObjectPropertyElementMain
SCOPE_GLOBAL = fourcc("glob")
SCOPE_INPUT = fourcc("inpt")
PROP_DEVICES = fourcc("dev#")
PROP_DEFAULT_INPUT = fourcc("dIn ")
PROP_DEFAULT_OUTPUT = fourcc("dOut")
PROP_STREAMS = fourcc("stm#")
PROP_NAME = fourcc("lnam")
CF_UTF8 = 0x08000100  # kCFStringEncodingUTF8

#: Selectors whose change means "re-check the devices".
WATCHED = (PROP_DEVICES, PROP_DEFAULT_INPUT, PROP_DEFAULT_OUTPUT)


class _Address(ctypes.Structure):
    _fields_ = (
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    )


_ListenerProc = ctypes.CFUNCTYPE(
    ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_void_p
)


class CoreAudioWatcher:  # pragma: no cover - needs macOS
    """Live Core Audio view used by :class:`sombra.audio.macos.MacAudioSource`."""

    def __init__(self, ca: Any, cf: Any) -> None:
        self._ca = ca
        self._cf = cf
        self._proc: Any = None

    def start(self, on_change: Callable[[], None]) -> None:
        """Call ``on_change()`` (on a Core Audio thread) whenever devices change."""
        if self._proc is not None:
            return

        def listener(_obj: int, _n: int, _addrs: Any, _data: Any) -> int:
            try:
                on_change()
            except Exception:  # never raise into Core Audio
                log.exception("audio device listener failed")
            return 0

        self._proc = _ListenerProc(listener)  # keep a reference while registered
        for sel in WATCHED:
            err = self._ca.AudioObjectAddPropertyListener(
                SYSTEM_OBJECT,
                ctypes.byref(_Address(sel, SCOPE_GLOBAL, ELEMENT_MAIN)),
                self._proc,
                None,
            )
            if err:
                log.warning("AudioObjectAddPropertyListener(%#x) failed: OSStatus %s", sel, err)

    def stop(self) -> None:
        if self._proc is None:
            return
        for sel in WATCHED:
            self._ca.AudioObjectRemovePropertyListener(
                SYSTEM_OBJECT,
                ctypes.byref(_Address(sel, SCOPE_GLOBAL, ELEMENT_MAIN)),
                self._proc,
                None,
            )
        self._proc = None

    def input_names(self) -> set[str]:
        """Names of the devices with input streams that exist right now."""
        names = set()
        for dev in self._device_ids():
            if self._size(dev, PROP_STREAMS, SCOPE_INPUT) > 0:
                name = self._name(dev)
                if name:
                    names.add(name)
        return names

    def default_input_name(self) -> str | None:
        return self._name(self._u32(SYSTEM_OBJECT, PROP_DEFAULT_INPUT))

    def default_output_name(self) -> str | None:
        return self._name(self._u32(SYSTEM_OBJECT, PROP_DEFAULT_OUTPUT))

    # --- Core Audio plumbing -----------------------------------------------------------

    def _size(self, obj: int, sel: int, scope: int = SCOPE_GLOBAL) -> int:
        size = ctypes.c_uint32(0)
        addr = _Address(sel, scope, ELEMENT_MAIN)
        err = self._ca.AudioObjectGetPropertyDataSize(
            obj, ctypes.byref(addr), 0, None, ctypes.byref(size)
        )
        return 0 if err else size.value

    def _device_ids(self) -> list[int]:
        n = self._size(SYSTEM_OBJECT, PROP_DEVICES) // 4
        if n == 0:
            return []
        ids = (ctypes.c_uint32 * n)()
        size = ctypes.c_uint32(ctypes.sizeof(ids))
        addr = _Address(PROP_DEVICES, SCOPE_GLOBAL, ELEMENT_MAIN)
        err = self._ca.AudioObjectGetPropertyData(
            SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size), ids
        )
        return [] if err else list(ids[: size.value // 4])

    def _u32(self, obj: int, sel: int) -> int:
        value = ctypes.c_uint32(0)
        size = ctypes.c_uint32(4)
        addr = _Address(sel, SCOPE_GLOBAL, ELEMENT_MAIN)
        err = self._ca.AudioObjectGetPropertyData(
            obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(value)
        )
        return 0 if err else value.value

    def _name(self, dev: int) -> str | None:
        if not dev:
            return None
        ref = ctypes.c_void_p()
        size = ctypes.c_uint32(ctypes.sizeof(ref))
        addr = _Address(PROP_NAME, SCOPE_GLOBAL, ELEMENT_MAIN)
        err = self._ca.AudioObjectGetPropertyData(
            dev, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(ref)
        )
        if err or not ref.value:
            return None
        try:
            buf = ctypes.create_string_buffer(1024)
            if not self._cf.CFStringGetCString(ref, buf, len(buf), CF_UTF8):
                return None
            return buf.value.decode("utf-8")
        finally:
            self._cf.CFRelease(ref)


def _framework(name: str) -> Any:  # pragma: no cover - needs macOS
    path = ctypes.util.find_library(name)
    if path is None:
        raise OSError(f"{name} framework not found (Core Audio is macOS only)")
    return ctypes.cdll.LoadLibrary(path)


def create_watcher() -> CoreAudioWatcher:  # pragma: no cover - needs macOS
    ca = _framework("CoreAudio")
    cf = _framework("CoreFoundation")
    ca.AudioObjectAddPropertyListener.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(_Address),
        _ListenerProc,
        ctypes.c_void_p,
    ]
    ca.AudioObjectRemovePropertyListener.argtypes = ca.AudioObjectAddPropertyListener.argtypes
    ca.AudioObjectGetPropertyDataSize.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(_Address),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    ca.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(_Address),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    for fn in (
        ca.AudioObjectAddPropertyListener,
        ca.AudioObjectRemovePropertyListener,
        ca.AudioObjectGetPropertyDataSize,
        ca.AudioObjectGetPropertyData,
    ):
        fn.restype = ctypes.c_int32
    cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint32,
    ]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
    return CoreAudioWatcher(ca, cf)
