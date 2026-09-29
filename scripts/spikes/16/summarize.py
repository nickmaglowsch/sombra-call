#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Turn the spike #16 JSONL logs into the Markdown tables of docs/spikes/16-wayland.md.

    python3 scripts/spikes/16/summarize.py                  # every log in logs/
    python3 scripts/spikes/16/summarize.py logs/capture-stream-*.jsonl

Paste the output into the report's "Results" section as it is; do not edit numbers.
"""

from __future__ import annotations

import itertools
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from common import LOG_DIR


def load(path: Path) -> list[dict[str, Any]]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def first(events: list[dict[str, Any]], type_: str) -> dict[str, Any]:
    return next((e for e in events if e["type"] == type_), {})


def of_type(events: list[dict[str, Any]], type_: str) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == type_]


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def table(header: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join("" if c is None else str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def setup(events: list[dict[str, Any]]) -> str:
    env = first(events, "environment")
    if not env:
        return ""
    pkgs = "; ".join(env.get("portal_pkgs") or [])
    return (
        f"- OS: {env.get('os')} (kernel {env.get('kernel')}), session "
        f"{env.get('session_type')}/{env.get('desktop')}, {env.get('gnome_shell')}\n"
        f"- CPU: {env.get('cpu')}\n- Packages: {pkgs}\n"
    )


def capture(path: Path, events: list[dict[str, Any]]) -> str:
    summary = first(events, "summary")
    starts = of_type(events, "screencast_start")
    shots = of_type(events, "capture") + of_type(events, "screenshot")
    times = [e["t"] for e in shots]
    gaps = [b - a for a, b in itertools.pairwise(times)]
    out = [f"### {path.name}\n", setup(events)]
    versions = first(events, "portal_versions")
    if versions:
        shown = {k: v for k, v in versions.items() if k not in ("ts", "t", "type")}
        out.append(f"- Portal versions: {json.dumps(shown)}\n")
    out.append(
        table(
            ["metric", "value"],
            [
                ["mode", summary.get("mode")],
                ["captures", len(shots)],
                ["fresh frames", summary.get("fresh_frames")],
                ["span (min)", round((times[-1] - times[0]) / 60, 1) if times else None],
                ["max gap between captures (s)", round(max(gaps), 1) if gaps else None],
                ["ScreenCast starts", len(starts)],
                ["starts with restore_token sent", sum(s["restore_token_sent"] for s in starts)],
                [
                    "starts that returned a new token",
                    sum(s["restore_token_returned"] for s in starts),
                ],
                ["Start() p50 / max (s)", _p50_max([s["start_s"] for s in starts])],
                ["per-capture total p50 / p95 (s)", _p50_p95([e.get("total_s") for e in shots])],
                ["dialogs seen (marked by tester)", summary.get("dialogs_marked")],
                ["sessions closed by portal", len(of_type(events, "session_closed_by_portal"))],
                ["portal errors", "; ".join(e["error"] for e in of_type(events, "portal_error"))],
                ["CPU avg (% of one core)", summary.get("cpu_percent")],
                ["exit code", summary.get("exit_code")],
            ],
        )
    )
    notes = [f"- t={e['t']} s: {e['text']}" for e in of_type(events, "note")]
    if notes:
        out.append("\nTester notes:\n" + "\n".join(notes))
    return "\n".join(out) + "\n"


def _p50_max(values: list[float]) -> str | None:
    return f"{statistics.median(values):.2f} / {max(values):.2f}" if values else None


def _p50_p95(raw: list[Any]) -> str | None:
    values = [v for v in raw if isinstance(v, int | float)]
    return f"{statistics.median(values):.2f} / {pct(values, 0.95):.2f}" if values else None


def notify(path: Path, events: list[dict[str, Any]]) -> str:
    server = first(events, "fdo_server")
    portal = first(events, "portal_notification")
    rows = [
        [
            t["n"],
            t["expected"],
            t["action"],
            t["matched"],
            t["action_s"],
            t["activation_token"],
            t.get("edit_window"),
            t.get("window_focused_s"),
            t.get("inline_reply"),
        ]
        for t in of_type(events, "trial")
    ]
    return (
        f"### {path.name}\n\n{setup(events)}"
        f"- fdo server: {server.get('server')}, capabilities {server.get('capabilities')}, "
        f"show-banners {server.get('show_banners')}\n"
        f"- portal Notification: version {portal.get('version')}, "
        f"options {portal.get('supported_options')}\n\n"
        + table(
            [
                "#",
                "expected",
                "got",
                "ok",
                "latency s",
                "activation token",
                "edit window",
                "focused after s",
                "inline reply",
            ],
            rows,
        )
        + "\n"
    )


def audio(path: Path, events: list[dict[str, Any]]) -> str:
    devices = first(events, "devices")
    rows = []
    for e in of_type(events, "phase"):
        mic, mon = e.get("mic", {}), e.get("monitor", {})
        rows.append(
            [
                e["phase"],
                mic.get("rms_dbfs", mic.get("error")),
                mon.get("rms_dbfs", mon.get("error")),
                e.get("separation_db"),
                mic.get("seconds"),
                mon.get("seconds"),
                e["wall_s"],
                bool(mic.get("clipped") or mon.get("clipped")),
            ]
        )
    return (
        f"### {path.name}\n\n{setup(events)}"
        f"- output {devices.get('output')}, backend {devices.get('backend')}, "
        f"source `{devices.get('source')}`, sink `{devices.get('sink')}`, "
        f"echo-cancel loaded {devices.get('echo_cancel_loaded')}\n\n"
        + table(
            [
                "phase",
                "mic RMS dBFS",
                "monitor RMS dBFS",
                "separation dB",
                "mic s",
                "monitor s",
                "wall s",
                "clipped",
            ],
            rows,
        )
        + "\n"
    )


def window(path: Path, events: list[dict[str, Any]]) -> str:
    methods = ["introspect", "eval", "atspi", "extension", "window_calls"]
    rows = []
    for e in of_type(events, "sample"):
        cells = []
        for m in methods:
            r = e.get(m, {})
            cells.append(
                f"error: {r['error'].removeprefix('GDBus.Error:')[:70]}"
                if "error" in r
                else f"{r.get('app')} / {r.get('title')}"
            )
        rows.append([e["target"], *cells])
    settings = first(events, "settings")
    return (
        f"### {path.name}\n\n{setup(events)}"
        f"- toolkit-accessibility {settings.get('toolkit_accessibility')}, "
        f"extensions {settings.get('enabled_extensions')}\n\n"
        + table(["clicked window", *methods], rows)
        + "\n"
    )


HANDLERS = {"capture": capture, "notify": notify, "audio": audio, "window-title": window}


def summarize(paths: list[Path]) -> str:
    parts = []
    for path in sorted(paths):
        kind = next((k for k in HANDLERS if path.name.startswith(k)), None)
        if kind is not None:
            parts.append(HANDLERS[kind](path, load(path)))
    return "\n".join(parts) if parts else "no logs found\n"


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] or sorted(LOG_DIR.glob("*.jsonl"))
    sys.stdout.write(summarize(paths))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
