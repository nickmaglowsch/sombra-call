#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []  # python3-gi and gir1.2-gtk-4.0 come from apt (README.md)
# ///
"""Spike #16, question 2: approve / edit / discard from a GNOME notification.

Sends one notification per trial with three buttons (Aprovar, Editar, Descartar) and a
default action (click on the body). The console tells you which button to click; the log
records what arrived, how fast, and whether GNOME sent an XDG activation token first.

``Editar`` (and a click on the body) opens the local-UI fallback: a small GTK 4 window with
the suggestion in an editable text box. The log records whether that window got focus
(GNOME focus-stealing prevention) and the edited text length.

Backends:

* ``fdo``    (default) org.freedesktop.Notifications, what GNOME Shell implements directly.
* ``portal`` org.freedesktop.portal.Notification v2 (buttons, and inline reply if the
             server advertises the ``im.reply-with-text`` purpose). Host apps usually need
             ``--app-id`` plus an installed .desktop file (README.md).
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import gi

gi.require_version("Gio", "2.0")
from common import JsonlLog, environment  # noqa: E402 - after gi setup, same reason
from gi.repository import Gio, GLib  # noqa: E402 - gi.require_version must run first
from portal import NOTIFICATION, PORTAL_BUS, PORTAL_PATH, Portal  # noqa: E402 - same reason

FDO_BUS = "org.freedesktop.Notifications"
FDO_PATH = "/org/freedesktop/Notifications"
TITLE = "Sombra: sugestão pronta"
BODY = (
    "Pergunta: “Nick, o que você acha desse gráfico?”\n"
    "Sugestão: O pico em agosto é a migração do billing; sem ele o crescimento fica em 4%."
)
LABELS = {"approve": "Aprovar", "edit": "Editar", "discard": "Descartar"}
DEFAULT_PLAN = "approve,edit,discard,default,approve,edit,discard,ignore"
CLOSE_REASONS = {1: "expired", 2: "dismissed", 3: "closed_by_app", 4: "undefined"}


class Trial:
    def __init__(self, n: int, expected: str) -> None:
        self.n = n
        self.expected = expected
        self.notification_id: Any = None
        self.sent = 0.0
        self.action: str | None = None
        self.action_s: float | None = None
        self.token: str | None = None
        self.reply: str | None = None
        self.closed: str | None = None


class Run:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.log = JsonlLog(f"notify-{args.backend}")
        self.bus: Gio.DBusConnection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.portal = Portal(self.bus)
        self.loop = GLib.MainLoop()
        self.trial: Trial | None = None
        self.reply_purpose = False

    # --- probes ---------------------------------------------------------------------

    def probe(self) -> None:
        self.log.event("environment", **environment())
        if self.args.app_id:
            result = self.portal.register_host_app(self.args.app_id)
            self.log.event("host_registry", app_id=self.args.app_id, result=result)
        caps = self._fdo_call("GetCapabilities", None, "(as)")
        info = self._fdo_call("GetServerInformation", None, "(ssss)")
        banners = _gsetting("org.gnome.desktop.notifications", "show-banners")
        self.log.event("fdo_server", capabilities=caps, server=info, show_banners=banners)
        if banners is False:
            print("WARNING: Do Not Disturb is on (show-banners=false); banners will not pop up")
        options = self.portal.get_property(NOTIFICATION, "SupportedOptions")
        version = self.portal.get_property(NOTIFICATION, "version")
        self.log.event("portal_notification", version=version, supported_options=options)
        purposes = (options or {}).get("button-purpose", [])
        self.reply_purpose = "im.reply-with-text" in purposes

    def _fdo_call(self, method: str, params: GLib.Variant | None, reply: str) -> Any:
        try:
            result = self.bus.call_sync(
                FDO_BUS,
                FDO_PATH,
                FDO_BUS,
                method,
                params,
                GLib.VariantType(reply),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except GLib.Error as exc:
            return f"error: {exc.message}"
        value = result.unpack()
        return value[0] if len(value) == 1 else value

    # --- sending --------------------------------------------------------------------

    def send_fdo(self, trial: Trial) -> None:
        actions = ["default", "Abrir"]
        for key, label in LABELS.items():
            actions += [key, label]
        hints: dict[str, GLib.Variant] = {"urgency": GLib.Variant("y", self.args.urgency)}
        if self.args.app_id:
            hints["desktop-entry"] = GLib.Variant("s", self.args.app_id)
        params = GLib.Variant(
            "(susssasa{sv}i)",
            ("Sombra (spike)", 0, "dialog-question", TITLE, BODY, actions, hints, 0),
        )
        result = self._fdo_call("Notify", params, "(u)")
        if isinstance(result, str):
            self.log.event("send_error", n=trial.n, error=result)
            result = None
        trial.notification_id = result

    def send_portal(self, trial: Trial) -> None:
        buttons = []
        for key, label in LABELS.items():
            button = {"label": GLib.Variant("s", label), "action": GLib.Variant("s", key)}
            if key == "edit" and self.reply_purpose:
                button["purpose"] = GLib.Variant("s", "im.reply-with-text")
            buttons.append(button)
        notification: dict[str, GLib.Variant] = {
            "title": GLib.Variant("s", TITLE),
            "body": GLib.Variant("s", BODY),
            "priority": GLib.Variant("s", "high"),
            "default-action": GLib.Variant("s", "default"),
            "buttons": GLib.Variant("aa{sv}", buttons),
        }
        if self.reply_purpose:
            notification["category"] = GLib.Variant("s", "im.received")
        trial.notification_id = f"sombra-{trial.n}"
        try:
            self.bus.call_sync(
                PORTAL_BUS,
                PORTAL_PATH,
                NOTIFICATION,
                "AddNotification",
                GLib.Variant("(sa{sv})", (trial.notification_id, notification)),
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except GLib.Error as exc:
            self.log.event("send_error", n=trial.n, error=exc.message)
            trial.notification_id = None

    def withdraw(self, trial: Trial) -> None:
        """Remove the notification so trials do not pile up in the tray."""
        if self.args.backend == "fdo":
            self._fdo_call("CloseNotification", GLib.Variant("(u)", (trial.notification_id,)), "()")
            return
        try:
            self.bus.call_sync(
                PORTAL_BUS,
                PORTAL_PATH,
                NOTIFICATION,
                "RemoveNotification",
                GLib.Variant("(s)", (trial.notification_id,)),
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except GLib.Error as exc:
            self.log.event("withdraw_error", n=trial.n, error=exc.message)

    # --- signals --------------------------------------------------------------------

    def subscribe(self) -> None:
        if self.args.backend == "fdo":
            for member in ("ActionInvoked", "ActivationToken", "NotificationClosed"):
                self.bus.signal_subscribe(
                    None, FDO_BUS, member, FDO_PATH, None, Gio.DBusSignalFlags.NONE, self._on_fdo
                )
        else:
            self.bus.signal_subscribe(
                PORTAL_BUS,
                NOTIFICATION,
                "ActionInvoked",
                PORTAL_PATH,
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_portal,
            )

    def _on_fdo(self, *args: Any) -> None:
        member, params = args[4], args[5].unpack()
        trial = self.trial
        if trial is None or params[0] != trial.notification_id:
            return
        if member == "ActivationToken":
            trial.token = params[1]
        elif member == "ActionInvoked":
            self._action(trial, params[1])
        elif member == "NotificationClosed":
            trial.closed = CLOSE_REASONS.get(params[1], str(params[1]))
            if trial.action is None:
                self.loop.quit()

    def _on_portal(self, *args: Any) -> None:
        notification_id, action, parameter = args[5].unpack()
        trial = self.trial
        if trial is None or notification_id != trial.notification_id:
            return
        token, trial.reply = parse_portal_parameter(parameter)
        trial.token = trial.token or token
        self._action(trial, action)

    def _action(self, trial: Trial, action: str) -> None:
        if trial.action is None:
            trial.action = action
            trial.action_s = time.monotonic() - trial.sent
            self.loop.quit()

    # --- trials ---------------------------------------------------------------------

    def run(self) -> int:
        self.probe()
        self.subscribe()
        plan = [p.strip() for p in self.args.plan.split(",") if p.strip()]
        for n, expected in enumerate(plan, start=1):
            self.run_trial(Trial(n, expected))
        self.log.event("summary", trials=len(plan), cpu_percent=self.log.cpu_percent())
        self.log.close()
        return 0

    def run_trial(self, trial: Trial) -> None:
        hint = {
            "default": "click the notification BODY (not a button)",
            "ignore": f"do nothing for {self.args.timeout:.0f} s",
        }.get(trial.expected, f"click the “{LABELS.get(trial.expected, trial.expected)}” button")
        input(f"\ntrial {trial.n}: press Enter, then {hint} ")
        self.trial = trial
        trial.sent = time.monotonic()
        (self.send_fdo if self.args.backend == "fdo" else self.send_portal)(trial)
        if trial.notification_id is not None:
            run_loop(self.loop, self.args.timeout)
            self.withdraw(trial)
        edit: dict[str, Any] = {}
        if trial.action in ("edit", "default") and not self.args.no_window:
            edit = edit_window(trial.token, self.args.timeout)
        self.log.event(
            "trial",
            n=trial.n,
            expected=trial.expected,
            action=trial.action,
            matched=(trial.action or "ignore") == trial.expected,
            action_s=round(trial.action_s, 3) if trial.action_s is not None else None,
            activation_token=trial.token is not None,
            closed=trial.closed,
            inline_reply=trial.reply,
            **edit,
        )
        self.trial = None


def parse_portal_parameter(parameter: Any) -> tuple[str | None, str | None]:
    """Split the portal ``ActionInvoked`` parameter array into (activation token, reply).

    Per the v2 docs the array holds, in order and each only if applicable: the action
    ``target``, ``platform-data`` (a{sv} with ``activation-token``) and the purpose's user
    ``response``. We never set a target, and v1 backends send an empty array, so walk it
    by type: the first dict is platform data, a string after it is the inline reply.
    """
    token: str | None = None
    reply: str | None = None
    seen_platform_data = False
    for item in parameter or []:
        if isinstance(item, dict) and not seen_platform_data:
            seen_platform_data = True
            value = item.get("activation-token")
            token = value if isinstance(value, str) else None
        elif isinstance(item, str) and seen_platform_data:
            reply = item
    return token, reply


def edit_window(token: str | None, timeout_s: float) -> dict[str, Any]:
    """The local-UI fallback for "edit": a GTK 4 window with the suggestion editable."""
    try:
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
    except (ImportError, ValueError) as exc:
        return {"edit_window": f"unavailable: {exc}"}
    if not Gtk.init_check():
        return {"edit_window": "unavailable: no display"}
    loop = GLib.MainLoop()
    opened = time.monotonic()
    out: dict[str, Any] = {"edit_window": "opened", "window_focused_s": None}
    win = Gtk.Window(title="Sombra: editar resposta", default_width=520, default_height=220)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, vexpand=True)
    view.get_buffer().set_text(BODY.split("Sugestão: ", 1)[1])
    send = Gtk.Button(label="Enviar")
    box.append(view)
    box.append(send)
    win.set_child(box)

    def on_active(*_: Any) -> None:
        if win.props.is_active and out["window_focused_s"] is None:
            out["window_focused_s"] = round(time.monotonic() - opened, 3)

    def on_send(*_: Any) -> None:
        buffer = view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        out["edit_window"], out["edited_chars"] = "sent", len(text)
        out["edit_s"] = round(time.monotonic() - opened, 3)
        win.close()

    win.connect("notify::is-active", on_active)
    win.connect("close-request", lambda *_: loop.quit() or False)
    send.connect("clicked", on_send)
    if token:
        win.set_startup_id(token)  # XDG activation: lets GNOME give the window focus
    win.present()
    run_loop(loop, timeout_s * 2)
    win.destroy()
    return out


def run_loop(loop: GLib.MainLoop, timeout_s: float) -> bool:
    """Run until something quits the loop or the timeout passes. True if it timed out."""
    fired: list[bool] = []

    def on_timeout() -> bool:
        fired.append(True)
        loop.quit()
        return False

    timer = GLib.timeout_add(int(timeout_s * 1000), on_timeout)
    loop.run()
    if not fired:
        GLib.source_remove(timer)
    return bool(fired)


def _gsetting(schema: str, key: str) -> Any:
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup(schema, True) is None:
        return None
    return Gio.Settings.new(schema).get_value(key).unpack()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--backend", choices=["fdo", "portal"], default="fdo")
    parser.add_argument("--plan", default=DEFAULT_PLAN, help="comma list of expected actions")
    parser.add_argument("--timeout", type=float, default=45.0, help="seconds to wait per trial")
    parser.add_argument("--urgency", type=int, choices=[0, 1, 2], default=1, help="fdo urgency")
    parser.add_argument("--app-id", help="desktop-entry hint / host registry app id")
    parser.add_argument("--no-window", action="store_true", help="skip the GTK edit window")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return Run(parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
