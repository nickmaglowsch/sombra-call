"""``sombra windows`` output and error paths, with a fake window lister."""

from __future__ import annotations

import argparse
import io
import json

from sombra.cli import build_parser
from sombra.screen.commands import format_windows, run_windows
from sombra.screen.macos import ScreenPermissionError, WindowInfo

WINDOWS = [
    WindowInfo(id=10, app="zoom.us", title="Zoom Meeting", pid=1, layer=0, on_screen=True),
    WindowInfo(id=1234, app="Google Chrome", title=None, pid=2, layer=0, on_screen=True),
    WindowInfo(id=13, app="Slack", title="general", pid=3, layer=0, on_screen=False),
]


def test_registered_in_cli() -> None:
    args = build_parser().parse_args(["windows", "--json"])
    assert args.func is run_windows
    assert args.json is True


def test_table_output() -> None:
    out, err = io.StringIO(), io.StringIO()
    code = run_windows(
        argparse.Namespace(json=False), platform="darwin", lister=lambda: WINDOWS, out=out, err=err
    )
    assert code == 0
    assert err.getvalue() == ""
    assert out.getvalue().splitlines() == [
        "  ID  APP            TITLE",
        "  10  zoom.us        Zoom Meeting",
        "1234  Google Chrome  -",
        "  13  Slack          general  (hidden)",
    ]


def test_json_output() -> None:
    lines = format_windows(WINDOWS, as_json=True).splitlines()
    assert [json.loads(line) for line in lines][1] == {
        "id": 1234,
        "app": "Google Chrome",
        "title": None,
        "on_screen": True,
    }


def test_empty() -> None:
    assert format_windows([], as_json=False) == "no capturable windows\n"
    assert format_windows([], as_json=True) == ""


def test_not_macos() -> None:
    err = io.StringIO()
    code = run_windows(argparse.Namespace(json=False), platform="linux", err=err)
    assert code == 2
    assert "macOS" in err.getvalue()


def test_permission_error_is_reported() -> None:
    def denied() -> list[WindowInfo]:
        raise ScreenPermissionError()

    err = io.StringIO()
    code = run_windows(
        argparse.Namespace(json=False), platform="darwin", lister=denied, out=io.StringIO(), err=err
    )
    assert code == 1
    assert "Screen Recording" in err.getvalue()
