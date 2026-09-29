# /// script
# requires-python = ">=3.10"
# dependencies = []  # PyGObject comes from apt: python3-gi (see README.md)
# ///
"""Minimal xdg-desktop-portal client on GDBus, shared by the issue #16 spike scripts.

Implements the Request/Response pattern from the portal docs: subscribe to
``org.freedesktop.portal.Request::Response`` on the predicted handle path *before*
calling the method, then run a GLib main loop until the response arrives.
"""

from __future__ import annotations

import contextlib
import secrets
import time
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402 - gi.require_version must run first

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"
SCREENSHOT = "org.freedesktop.portal.Screenshot"
NOTIFICATION = "org.freedesktop.portal.Notification"
REGISTRY = "org.freedesktop.host.portal.Registry"

# ScreenCast constants (org.freedesktop.portal.ScreenCast docs)
SOURCE_MONITOR, SOURCE_WINDOW, SOURCE_VIRTUAL = 1, 2, 4
CURSOR_HIDDEN, CURSOR_EMBEDDED, CURSOR_METADATA = 1, 2, 4
PERSIST_NONE, PERSIST_WHILE_RUNNING, PERSIST_UNTIL_REVOKED = 0, 1, 2
RESPONSE_NAMES = {0: "success", 1: "cancelled", 2: "other"}


class PortalError(RuntimeError):
    pass


class Portal:
    """Blocking helpers around one session-bus connection."""

    def __init__(self, bus: Gio.DBusConnection | None = None) -> None:
        self.bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        unique = self.bus.get_unique_name() or ""
        # Request handles are /org/freedesktop/portal/desktop/request/SENDER/TOKEN where
        # SENDER is the unique name without ':' and with '.' replaced by '_'.
        self._sender = unique.lstrip(":").replace(".", "_")

    # --- generic ------------------------------------------------------------------

    def get_property(self, interface: str, name: str) -> Any:
        """Read a portal property; returns None when the interface or property is missing."""
        try:
            reply = self.bus.call_sync(
                PORTAL_BUS,
                PORTAL_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", (interface, name)),
                GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except GLib.Error:
            return None
        return reply.unpack()[0]

    def register_host_app(self, app_id: str) -> str:
        """org.freedesktop.host.portal.Registry.Register (xdg-desktop-portal >= 1.19.4).

        Must happen before any other portal call on this connection. Returns "ok" or the
        error text (older portals do not have the interface).
        """
        try:
            self.bus.call_sync(
                PORTAL_BUS,
                PORTAL_PATH,
                REGISTRY,
                "Register",
                GLib.Variant("(sa{sv})", (app_id, {})),
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except GLib.Error as exc:
            return f"error: {exc.message}"
        return "ok"

    def request(
        self,
        interface: str,
        method: str,
        signature: str,
        args: tuple[Any, ...],
        options: dict[str, GLib.Variant],
        timeout_s: float = 300.0,
    ) -> tuple[int, dict[str, Any], float]:
        """Call a Request-returning portal method and wait for its Response.

        ``signature`` covers the positional ``args`` only; the trailing ``a{sv}`` options
        dict is appended here. Returns (response code, results, seconds waited).
        """
        token = f"sombra_{secrets.token_hex(6)}"
        options = {**options, "handle_token": GLib.Variant("s", token)}
        expected = f"{PORTAL_PATH}/request/{self._sender}/{token}"
        loop = GLib.MainLoop()
        box: dict[str, Any] = {}

        def on_response(
            _conn: Any, _sender: Any, _path: Any, _iface: Any, _sig: Any, params: GLib.Variant
        ) -> None:
            code, results = params.unpack()
            box["code"], box["results"] = code, results
            loop.quit()

        sub = self._subscribe_response(expected, on_response)
        started = time.monotonic()
        try:
            reply = self.bus.call_sync(
                PORTAL_BUS,
                PORTAL_PATH,
                interface,
                method,
                GLib.Variant(f"({signature}a{{sv}})", (*args, options)),
                GLib.VariantType("(o)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            handle = reply.unpack()[0]
            if handle != expected:  # very old portals: follow the returned handle instead
                self.bus.signal_unsubscribe(sub)
                sub = self._subscribe_response(handle, on_response)
            if "code" not in box:
                timer = GLib.timeout_add(int(timeout_s * 1000), loop.quit)
                loop.run()
                if "code" in box:
                    GLib.source_remove(timer)
        finally:
            self.bus.signal_unsubscribe(sub)
        waited = time.monotonic() - started
        if "code" not in box:
            raise PortalError(f"{interface}.{method}: no Response within {timeout_s:.0f} s")
        return int(box["code"]), dict(box["results"]), waited

    def _subscribe_response(self, path: str, callback: Callable[..., None]) -> int:
        return int(
            self.bus.signal_subscribe(
                PORTAL_BUS,
                "org.freedesktop.portal.Request",
                "Response",
                path,
                None,
                Gio.DBusSignalFlags.NONE,
                callback,
            )
        )

    # --- ScreenCast -----------------------------------------------------------------

    def screencast_create_session(self) -> str:
        options = {"session_handle_token": GLib.Variant("s", f"sombra_{secrets.token_hex(6)}")}
        code, results, _ = self.request(SCREENCAST, "CreateSession", "", (), options)
        if code != 0:
            raise PortalError(f"CreateSession: {RESPONSE_NAMES.get(code, code)}")
        return str(results["session_handle"])

    def screencast_select_sources(
        self,
        session: str,
        *,
        types: int = SOURCE_MONITOR,
        cursor_mode: int = CURSOR_EMBEDDED,
        persist_mode: int = PERSIST_UNTIL_REVOKED,
        restore_token: str | None = None,
    ) -> None:
        options: dict[str, GLib.Variant] = {
            "types": GLib.Variant("u", types),
            "multiple": GLib.Variant("b", False),
            "cursor_mode": GLib.Variant("u", cursor_mode),
            "persist_mode": GLib.Variant("u", persist_mode),
        }
        if restore_token:
            options["restore_token"] = GLib.Variant("s", restore_token)
        code, _, _ = self.request(SCREENCAST, "SelectSources", "o", (session,), options)
        if code != 0:
            raise PortalError(f"SelectSources: {RESPONSE_NAMES.get(code, code)}")

    def screencast_start(self, session: str) -> tuple[dict[str, Any], float]:
        """Start; returns (results, seconds). A dialog, if shown, is inside those seconds."""
        code, results, waited = self.request(SCREENCAST, "Start", "os", (session, ""), {})
        if code != 0:
            raise PortalError(f"Start: {RESPONSE_NAMES.get(code, code)}")
        return results, waited

    def screencast_open_pipewire_remote(self, session: str) -> int:
        reply, fd_list = self.bus.call_with_unix_fd_list_sync(
            PORTAL_BUS,
            PORTAL_PATH,
            SCREENCAST,
            "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (session, {})),
            GLib.VariantType("(h)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )
        return int(fd_list.get(reply.unpack()[0]))

    def close_session(self, session: str) -> None:
        with contextlib.suppress(GLib.Error):  # already closed by the portal or the user
            self.bus.call_sync(
                PORTAL_BUS,
                session,
                "org.freedesktop.portal.Session",
                "Close",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )

    def on_session_closed(self, session: str, callback: Callable[[], None]) -> int:
        return int(
            self.bus.signal_subscribe(
                PORTAL_BUS,
                "org.freedesktop.portal.Session",
                "Closed",
                session,
                None,
                Gio.DBusSignalFlags.NONE,
                lambda *_: callback(),
            )
        )


def describe_streams(results: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Start()'s ``streams`` (a(ua{sv})) into JSON-friendly dicts."""
    return [{"node_id": node, **dict(props)} for node, props in results.get("streams", [])]
