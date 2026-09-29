#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []  # python3-gi, GStreamer and gstreamer1.0-pipewire come from apt (README.md)
# ///
"""Spike #16, question 1: periodic screen capture on GNOME Wayland without repeated dialogs.

Modes (all log to ``logs/capture-<mode>-<stamp>.jsonl``):

* ``stream``      one ScreenCast session, keep the PipeWire stream open, grab a frame every
                  ``--interval`` s for ``--minutes``. Tests "30 min without a new dialog".
* ``per-capture`` a new ScreenCast session per frame, restored from the last restore_token.
                  Tests token rotation and the "capture only on trigger" fallback.
* ``screenshot``  org.freedesktop.portal.Screenshot (non-interactive) every ``--interval`` s.

Every mode reads and rewrites ``--token-file``, so running ``stream`` twice tests "after
restarting the app". While it runs, type ``d`` + Enter each time you see a permission or
source-picker dialog, and any other text + Enter to add a note to the log.

``--dry-run`` swaps the portal for ``videotestsrc`` to check the GStreamer side without a
Wayland session.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from common import LOG_DIR, JsonlLog, environment  # noqa: E402 - after gi setup, same reason
from gi.repository import GdkPixbuf, GLib, Gst  # noqa: E402 - gi.require_version must run first
from portal import (  # noqa: E402 - after gi setup, same reason
    PERSIST_UNTIL_REVOKED,
    SCREENCAST,
    SCREENSHOT,
    SOURCE_MONITOR,
    SOURCE_WINDOW,
    Portal,
    PortalError,
    describe_streams,
)

TEST_SOURCE = "videotestsrc is-live=true pattern=ball ! video/x-raw,width=1920,height=1080"


class TokenStore:
    """The restore token lives in a file so a second run (an "app restart") can use it."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> str | None:
        if not self.path.exists():
            return None
        return self.path.read_text(encoding="utf-8").strip() or None

    def save(self, token: str | None) -> None:
        if token:
            self.path.write_text(token + "\n", encoding="utf-8")


class Grabber:
    """PipeWire (or test) source -> RGB appsink that keeps only the newest frame."""

    def __init__(self, source: str, max_fps: int, log: JsonlLog) -> None:
        rate = f"videorate drop-only=true max-rate={max_fps} ! " if max_fps > 0 else ""
        desc = (
            f"{source} ! {rate}videoconvert ! video/x-raw,format=RGB ! "
            "appsink name=sink max-buffers=1 drop=true sync=false"
        )
        self.pipeline = Gst.parse_launch(desc)
        self.sink = self.pipeline.get_by_name("sink")
        self.last: Gst.Sample | None = None
        self.log = log
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        log.event("pipeline", description=desc)

    def _on_error(self, _bus: Any, message: Gst.Message) -> None:
        err, debug = message.parse_error()
        self.log.event("pipeline_error", error=err.message, debug=debug)

    def start(self) -> None:
        self.pipeline.set_state(Gst.State.PLAYING)

    def grab(self, timeout_s: float = 0.0) -> tuple[Gst.Sample | None, bool]:
        """Newest frame since the last grab, else the previous one (fresh=False)."""
        sample = self.sink.emit("try-pull-sample", int(timeout_s * Gst.SECOND))
        if sample is not None:
            self.last = sample
            return sample, True
        return self.last, False

    def stop(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.get_bus().remove_signal_watch()


def pipewire_source(fd: int, node_id: int, keepalive_ms: int) -> str:
    # keepalive-time re-sends the last buffer when the compositor sends no damage, so a
    # static screen still yields frames. do-timestamp stamps buffers on arrival.
    keep = f" keepalive-time={keepalive_ms}" if keepalive_ms > 0 else ""
    return f"pipewiresrc fd={fd} path={node_id} do-timestamp=true always-copy=true{keep}"


def sample_size(sample: Gst.Sample) -> tuple[int, int]:
    caps = sample.get_caps().get_structure(0)
    return int(caps.get_value("width")), int(caps.get_value("height"))


def save_jpeg(sample: Gst.Sample, path: Path, max_width: int = 1280) -> int:
    """Encode like the frame pipeline will (~1280 px wide, q80). Returns bytes written."""
    width, height = sample_size(sample)
    buf = sample.get_buffer()
    ok, info = buf.map(Gst.MapFlags.READ)
    if not ok:
        return 0
    try:
        data = GLib.Bytes.new(bytes(info.data))
    finally:
        buf.unmap(info)
    stride = (width * 3 + 3) & ~3  # GStreamer pads RGB rows to 4 bytes
    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
        data, GdkPixbuf.Colorspace.RGB, False, 8, width, height, stride
    )
    if width > max_width:
        pixbuf = pixbuf.scale_simple(
            max_width, round(height * max_width / width), GdkPixbuf.InterpType.BILINEAR
        )
    pixbuf.savev(str(path), "jpeg", ["quality"], ["80"])
    return path.stat().st_size


class Run:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.log = JsonlLog(f"capture-{args.mode}{'-dry' if args.dry_run else ''}")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.frames = LOG_DIR / f"frames-{args.mode}-{stamp}"
        self.frames.mkdir(parents=True, exist_ok=True)
        self.tokens = TokenStore(args.token_file)
        self.loop = GLib.MainLoop()
        self.portal: Portal | None = None
        self.grabber: Grabber | None = None
        self.session: str | None = None
        self.captures = 0
        self.fresh = 0
        self.dialog_marks = 0
        self.exit_code = 0
        self.deadline = time.monotonic() + args.minutes * 60

    # --- setup ----------------------------------------------------------------------

    def setup(self) -> None:
        self.log.event("environment", **environment())
        self.log.event("config", **{k: str(v) for k, v in vars(self.args).items()})
        if self.args.dry_run:
            return
        self.portal = Portal()
        if self.args.app_id:
            result = self.portal.register_host_app(self.args.app_id)
            self.log.event("host_registry", app_id=self.args.app_id, result=result)
        self.log.event(
            "portal_versions",
            screencast=self.portal.get_property(SCREENCAST, "version"),
            source_types=self.portal.get_property(SCREENCAST, "AvailableSourceTypes"),
            cursor_modes=self.portal.get_property(SCREENCAST, "AvailableCursorModes"),
            screenshot=self.portal.get_property(SCREENSHOT, "version"),
        )

    def watch_stdin(self) -> None:
        if not sys.stdin.isatty():
            return
        channel = GLib.IOChannel.unix_new(sys.stdin.fileno())
        GLib.io_add_watch(channel, GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN, self._on_stdin)
        print("type 'd' + Enter whenever a dialog appears; other text + Enter adds a note")

    def _on_stdin(self, *_: Any) -> bool:
        line = sys.stdin.readline()
        if not line:
            return False
        text = line.strip()
        if text.lower() == "d":
            self.dialog_marks += 1
            self.log.event("dialog_seen", count=self.dialog_marks)
        elif text:
            self.log.event("note", text=text)
        return True

    # --- ScreenCast -----------------------------------------------------------------

    def open_screencast(self) -> tuple[int, int, float]:
        """CreateSession/SelectSources/Start/OpenPipeWireRemote. Returns (fd, node, secs)."""
        assert self.portal is not None  # noqa: S101 - programming error, not input validation
        began = time.monotonic()
        token = self.tokens.load()
        self.session = self.portal.screencast_create_session()
        self.portal.screencast_select_sources(
            self.session,
            types=SOURCE_WINDOW if self.args.window else SOURCE_MONITOR,
            persist_mode=self.args.persist_mode,
            restore_token=token,
        )
        results, start_s = self.portal.screencast_start(self.session)
        new_token = results.get("restore_token")
        self.tokens.save(new_token)
        streams = describe_streams(results)
        self.log.event(
            "screencast_start",
            restore_token_sent=token is not None,
            restore_token_returned=new_token is not None,
            token_changed=new_token is not None and new_token != token,
            start_s=round(start_s, 3),
            streams=streams,
        )
        self.portal.on_session_closed(self.session, self._on_session_closed)
        fd = self.portal.screencast_open_pipewire_remote(self.session)
        return fd, int(streams[0]["node_id"]), time.monotonic() - began

    def _on_session_closed(self) -> None:
        self.log.event("session_closed_by_portal", session=self.session)
        if self.args.mode == "stream":
            self.exit_code = 3
            self.loop.quit()

    def close_screencast(self) -> None:
        if self.portal and self.session:
            self.portal.close_session(self.session)
        self.session = None

    def source_for(self, fd: int, node: int) -> str:
        return pipewire_source(fd, node, self.args.keepalive_ms)

    # --- modes ----------------------------------------------------------------------

    def run(self) -> int:
        self.setup()
        self.watch_stdin()
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._interrupt)
        GLib.timeout_add(int(self.args.minutes * 60_000), self._done)
        tick = {"stream": self.tick_stream, "per-capture": self.tick_per_capture}.get(
            self.args.mode, self.tick_screenshot
        )
        try:
            if self.args.mode == "stream":
                if self.args.dry_run:
                    source = TEST_SOURCE
                else:
                    fd, node, _ = self.open_screencast()
                    source = self.source_for(fd, node)
                self.grabber = Grabber(source, self.args.max_fps, self.log)
                self.grabber.start()
            GLib.timeout_add(int(self.args.interval * 1000), tick)
            GLib.timeout_add(1000, lambda: tick() and False)  # first capture after 1 s
            self.loop.run()
        except PortalError as exc:
            self.log.event("portal_error", error=str(exc))
            self.exit_code = 2
        finally:
            if self.grabber:
                self.grabber.stop()
            self.close_screencast()
            self.log.event(
                "summary",
                mode=self.args.mode,
                captures=self.captures,
                fresh_frames=self.fresh,
                dialogs_marked=self.dialog_marks,
                cpu_percent=self.log.cpu_percent(),
                exit_code=self.exit_code,
            )
            self.log.close()
        return self.exit_code

    def _interrupt(self) -> bool:
        self.log.event("interrupted")
        self.loop.quit()
        return False

    def _done(self) -> bool:
        self.loop.quit()
        return False

    def _record(self, sample: Gst.Sample | None, fresh: bool, **extra: Any) -> None:
        self.captures += 1
        self.fresh += int(fresh)
        fields: dict[str, Any] = {"n": self.captures, "fresh": fresh, **extra}
        if sample is not None:
            fields["width"], fields["height"] = sample_size(sample)
            if (self.captures - 1) % self.args.save_every == 0:
                path = self.frames / f"f{self.captures:04d}.jpg"
                fields["saved"], fields["jpeg_bytes"] = path.name, save_jpeg(sample, path)
        else:
            fields["error"] = "no frame yet"
        if self.captures % max(1, round(60 / self.args.interval)) == 0:
            fields["cpu_percent"] = self.log.cpu_percent()
        self.log.event("capture", **fields)

    def tick_stream(self) -> bool:
        assert self.grabber is not None  # noqa: S101 - set up in run()
        started = time.monotonic()
        sample, fresh = self.grabber.grab()
        self._record(sample, fresh, grab_ms=round((time.monotonic() - started) * 1000, 1))
        return time.monotonic() < self.deadline

    def tick_per_capture(self) -> bool:
        fd = -1
        try:
            if self.args.dry_run:
                fd_source, open_s = TEST_SOURCE, 0.0
            else:
                fd, node, open_s = self.open_screencast()
                fd_source = self.source_for(fd, node)
            started = time.monotonic()
            grabber = Grabber(fd_source, 0, self.log)
            grabber.start()
            sample, fresh = grabber.grab(timeout_s=5.0)
            first_frame_s = time.monotonic() - started
            grabber.stop()
            self._record(
                sample,
                fresh,
                open_s=round(open_s, 3),
                first_frame_s=round(first_frame_s, 3),
                total_s=round(open_s + first_frame_s, 3),
            )
        except PortalError as exc:
            self.log.event("portal_error", error=str(exc))
            self.exit_code = 2
            self.loop.quit()
            return False
        finally:
            if fd >= 0:  # pipewiresrc dups the fd; ours would leak once per capture
                os.close(fd)
            self.close_screencast()
        return time.monotonic() < self.deadline

    def tick_screenshot(self) -> bool:
        if self.portal is None:
            self.log.event("portal_error", error="screenshot mode has no --dry-run")
            self.loop.quit()
            return False
        options = {"interactive": GLib.Variant("b", False), "modal": GLib.Variant("b", False)}
        try:
            code, results, waited = self.portal.request(
                SCREENSHOT, "Screenshot", "s", ("",), options
            )
        except PortalError as exc:
            self.log.event("portal_error", error=str(exc))
            self.loop.quit()
            return False
        self.captures += 1
        fields: dict[str, Any] = {"n": self.captures, "code": code, "wait_s": round(waited, 3)}
        uri = results.get("uri")
        if uri:
            src = Path(unquote(urlparse(uri).path))
            if src.exists():
                # Move the file the portal wrote for this request out of ~/Pictures.
                dest = self.frames / f"s{self.captures:04d}{src.suffix}"
                src.replace(dest)
                fields["saved"], fields["bytes"] = dest.name, dest.stat().st_size
        if self.captures % max(1, round(60 / self.args.interval)) == 0:
            fields["cpu_percent"] = self.log.cpu_percent()
        self.log.event("screenshot", **fields)
        return time.monotonic() < self.deadline


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mode", choices=["stream", "per-capture", "screenshot"], default="stream")
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between captures")
    parser.add_argument("--save-every", type=int, default=12, help="save every Nth frame as JPEG")
    parser.add_argument(
        "--max-fps", type=int, default=1, help="videorate cap before convert; 0=off"
    )
    parser.add_argument("--keepalive-ms", type=int, default=1000, help="pipewiresrc keepalive")
    parser.add_argument(
        "--persist-mode", type=int, default=PERSIST_UNTIL_REVOKED, choices=[0, 1, 2]
    )
    parser.add_argument("--window", action="store_true", help="share a window instead of a monitor")
    parser.add_argument(
        "--token-file", type=Path, default=LOG_DIR / "restore_token.txt", help="restore token store"
    )
    parser.add_argument("--app-id", help="register as this host app id first (xdp >= 1.19.4)")
    parser.add_argument("--dry-run", action="store_true", help="videotestsrc, no portal")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    Gst.init(None)
    return Run(args).run()


if __name__ == "__main__":
    sys.exit(main())
