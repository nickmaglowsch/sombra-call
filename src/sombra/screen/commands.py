"""``sombra windows``: list capturable windows to pick window-only mode (PRD S4)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from typing import TextIO

from sombra.screen.macos import ScreenCaptureError, WindowInfo


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "windows",
        help="list capturable windows (app, title, id) for window-only capture (macOS)",
    )
    p.add_argument("--json", action="store_true", help="one JSON object per line")
    p.set_defaults(func=run_windows)


def _list_windows() -> list[WindowInfo]:  # pragma: no cover - macOS only
    from sombra.screen.macos import MacScreenSource

    return MacScreenSource().list_windows()


def format_windows(windows: Sequence[WindowInfo], as_json: bool) -> str:
    if as_json:
        return "".join(
            json.dumps({"id": w.id, "app": w.app, "title": w.title, "on_screen": w.on_screen})
            + "\n"
            for w in windows
        )
    if not windows:
        return "no capturable windows\n"
    rows = [("ID", "APP", "TITLE")] + [
        (str(w.id), w.app or "-", (w.title or "-") + ("" if w.on_screen else "  (hidden)"))
        for w in windows
    ]
    wid = max(len(r[0]) for r in rows)
    wapp = max(len(r[1]) for r in rows)
    return "".join(f"{i:>{wid}}  {a:<{wapp}}  {t}".rstrip() + "\n" for i, a, t in rows)


def run_windows(
    args: argparse.Namespace,
    *,
    platform: str = sys.platform,
    lister: Callable[[], list[WindowInfo]] = _list_windows,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    if platform != "darwin":
        err.write("sombra windows: only supported on macOS for now\n")
        return 2
    try:
        windows = lister()
    except ScreenCaptureError as exc:
        err.write(f"sombra windows: {exc}\n")
        return 1
    out.write(format_windows(windows, args.json))
    return 0
