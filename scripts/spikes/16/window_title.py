#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []  # python3-gi and gir1.2-atspi-2.0 come from apt (README.md)
# ///
"""Spike #16, question 4: can Sombra learn the focused app / window title on GNOME Wayland?

For each target window you name, the script counts down so you can click that window, then
asks every candidate method what is focused and logs the answers side by side:

* ``introspect``  org.gnome.Shell.Introspect.GetWindows (allow-listed callers only)
* ``eval``        org.gnome.Shell.Eval (only works in GNOME Shell "unsafe mode")
* ``atspi``       the accessibility tree: the frame whose state set contains ACTIVE
* ``extension``   the spike's own Shell extension (``extension/``), if installed and enabled
* ``window_calls`` the third-party "Window Calls" extension, if installed

Compare each answer with the window you actually clicked; ``summarize.py`` tabulates them.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import gi

gi.require_version("Gio", "2.0")
from common import JsonlLog, environment  # noqa: E402 - after gi setup, same reason
from gi.repository import Gio, GLib  # noqa: E402 - gi.require_version must run first

SHELL = "org.gnome.Shell"
DEFAULT_TARGETS = "Google Meet (browser),Zoom,Slack or Teams,Terminal"


class Probes:
    def __init__(self) -> None:
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._atspi: Any = None
        self._atspi_error: str | None = None
        try:
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi

            Atspi.init()
            self._atspi = Atspi
        except (ImportError, ValueError) as exc:
            self._atspi_error = str(exc)

    def _call(self, name: str, path: str, iface: str, method: str, params: Any = None) -> Any:
        reply = self.bus.call_sync(
            name, path, iface, method, params, None, Gio.DBusCallFlags.NONE, 3000, None
        )
        return reply.unpack()

    def introspect(self) -> dict[str, Any]:
        (windows,) = self._call(
            SHELL, "/org/gnome/Shell/Introspect", f"{SHELL}.Introspect", "GetWindows"
        )
        focused = [w for w in windows.values() if w.get("has-focus")]
        if not focused:
            return {"windows": len(windows), "focused": None}
        w = focused[0]
        return {"windows": len(windows), "app": w.get("app-id"), "title": w.get("title")}

    def eval(self) -> dict[str, Any]:
        code = "global.display.focus_window ? global.display.focus_window.get_title() : null"
        ok, result = self._call(
            SHELL, "/org/gnome/Shell", SHELL, "Eval", GLib.Variant("(s)", (code,))
        )
        return {"ok": ok, "title": result if ok else None, "raw": result}

    def atspi(self) -> dict[str, Any]:
        if self._atspi is None:
            return {"error": f"Atspi unavailable: {self._atspi_error}"}
        atspi = self._atspi
        desktop = atspi.get_desktop(0)
        apps = desktop.get_child_count()
        for i in range(apps):
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            for j in range(app.get_child_count()):
                win = app.get_child_at_index(j)
                if win is not None and win.get_state_set().contains(atspi.StateType.ACTIVE):
                    return {"apps": apps, "app": app.get_name(), "title": win.get_name()}
        return {"apps": apps, "title": None}

    def extension(self) -> dict[str, Any]:
        (raw,) = self._call(
            SHELL, "/org/sombra/Spike/WindowInfo", "org.sombra.Spike.WindowInfo", "Focused"
        )
        return dict(json.loads(raw))

    def window_calls(self) -> dict[str, Any]:
        path, iface = "/org/gnome/Shell/Extensions/Windows", "org.gnome.Shell.Extensions.Windows"
        (raw,) = self._call(SHELL, path, iface, "List")
        windows = json.loads(raw)
        focused = next((w for w in windows if w.get("focus")), None)
        if focused is None:
            return {"windows": len(windows), "title": None}
        result = {"app": focused.get("wm_class"), "title": focused.get("title")}
        if result["title"] is None and "id" in focused:
            (title,) = self._call(
                SHELL, path, iface, "GetTitle", GLib.Variant("(u)", (focused["id"],))
            )
            result["title"] = title
        return result

    def all(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in ("introspect", "eval", "atspi", "extension", "window_calls"):
            started = time.monotonic()
            try:
                out[name] = getattr(self, name)()
            except GLib.Error as exc:
                out[name] = {"error": exc.message}
            except (ValueError, KeyError, TypeError) as exc:
                out[name] = {"error": f"{type(exc).__name__}: {exc}"}
            out[name]["ms"] = round((time.monotonic() - started) * 1000, 1)
        return out


def _gsetting(schema: str, key: str) -> Any:
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup(schema, True) is None:
        return None
    return Gio.Settings.new(schema).get_value(key).unpack()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--targets", default=DEFAULT_TARGETS, help="comma list of windows")
    parser.add_argument("--countdown", type=int, default=5, help="seconds to click the window")
    args = parser.parse_args(argv)

    log = JsonlLog("window-title")
    log.event("environment", **environment())
    log.event(
        "settings",
        toolkit_accessibility=_gsetting("org.gnome.desktop.interface", "toolkit-accessibility"),
        enabled_extensions=_gsetting("org.gnome.shell", "enabled-extensions"),
    )
    probes = Probes()
    for target in [t.strip() for t in args.targets.split(",") if t.strip()]:
        input(f"\nnext: {target}. Press Enter, then click that window within {args.countdown} s ")
        for left in range(args.countdown, 0, -1):
            print(f"  {left}…", flush=True)
            time.sleep(1)
        log.event("sample", target=target, **probes.all())
    log.event("summary", cpu_percent=log.cpu_percent())
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
