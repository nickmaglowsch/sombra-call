"""``sombra ui-demo``: drive the overlay with fake suggestions for manual testing."""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from typing import Any


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "ui-demo", help="show the approval overlay with fake suggestions (manual test)"
    )
    p.add_argument("--interval", type=float, default=6.0, help="seconds between suggestions")
    p.add_argument("--think", type=float, default=1.5, help="seconds in 'buscando contexto…'")
    p.add_argument("--rounds", type=int, default=None, help="stop after N suggestions")
    p.add_argument(
        "--browser",
        action="store_true",
        help="open a browser tab instead of the always-on-top window",
    )
    p.add_argument("--no-open", action="store_true", help="only print the URL")
    p.add_argument("--no-clipboard", action="store_true", help="do not copy approved text")
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    from sombra.ui import window

    use_window = not args.browser and not args.no_open and window.window_available()
    if not args.browser and not args.no_open and not use_window:
        sys.stderr.write("pywebview not installed (uv sync --extra window); using a browser tab\n")

    ready = threading.Event()
    stop = threading.Event()
    box: dict[str, Any] = {}
    thread = threading.Thread(target=_serve, args=(args, box, ready, stop), daemon=True)
    thread.start()
    ready.wait()
    if "error" in box:
        sys.stderr.write(f"ui-demo: {box['error']}\n")
        return 1
    url: str = box["url"]
    sys.stdout.write(f"overlay at {url}\n")
    sys.stdout.flush()

    try:
        if use_window:
            window.open_window(url)  # blocks on the main thread until closed
        else:
            if not args.no_open:
                window.open_in_browser(url)
            thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        thread.join(timeout=2)
    return 0


def _serve(
    args: argparse.Namespace, box: dict[str, Any], ready: threading.Event, stop: threading.Event
) -> None:
    asyncio.run(_serve_async(args, box, ready, stop))


async def _serve_async(
    args: argparse.Namespace, box: dict[str, Any], ready: threading.Event, stop: threading.Event
) -> None:
    from sombra.ui.demo import drive, report_actions
    from sombra.ui.overlay import OverlayUI
    from sombra.ui.system import no_clipboard, system_clipboard

    try:
        ui = OverlayUI(clipboard=no_clipboard if args.no_clipboard else system_clipboard)
        await ui.start()
    except Exception as e:  # report anything, or _run would wait on `ready` forever
        box["error"] = str(e) or type(e).__name__
        ready.set()
        return
    box["url"] = ui.url
    ready.set()

    def write(line: str) -> None:
        sys.stdout.write(line)
        sys.stdout.flush()

    tasks = [
        asyncio.create_task(
            drive(ui, interval=args.interval, think=args.think, rounds=args.rounds)
        ),
        asyncio.create_task(report_actions(ui, write)),
    ]
    await asyncio.to_thread(stop.wait)  # window closed or Ctrl-C; keep serving until then
    await ui.close()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
