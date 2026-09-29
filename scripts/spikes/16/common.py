# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Shared helpers for the issue #16 Wayland spike scripts (stdlib only).

Every script writes one JSON object per line to a log file so ``summarize.py`` can turn a
run into the tables in ``docs/spikes/16-wayland.md``. Each line has ``ts`` (local ISO-8601
with offset), ``t`` (seconds since the script started, monotonic) and ``type``.
"""

from __future__ import annotations

import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

LOG_DIR = Path(__file__).resolve().parent / "logs"


class JsonlLog:
    """Append-only JSONL log that also echoes a short line to stdout."""

    def __init__(self, name: str, log_dir: Path = LOG_DIR) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = log_dir / f"{name}-{stamp}.jsonl"
        self._fh: TextIO = self.path.open("a", encoding="utf-8")
        self._t0 = time.monotonic()
        self._cpu0 = _cpu_seconds()
        print(f"logging to {self.path}")

    def event(self, type_: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "t": round(time.monotonic() - self._t0, 3),
            "type": type_,
            **fields,
        }
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        brief = {k: v for k, v in record.items() if k not in ("ts",)}
        print(json.dumps(brief, ensure_ascii=False, default=str)[:300])
        return record

    def cpu_percent(self) -> float:
        """Average CPU of this process (all threads, incl. GStreamer) since start, % of one core."""
        wall = time.monotonic() - self._t0
        return round(100.0 * (_cpu_seconds() - self._cpu0) / wall, 2) if wall > 0 else 0.0

    def close(self) -> None:
        self._fh.close()


def _cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def run_text(cmd: list[str]) -> str:
    """Run a fixed, trusted diagnostic command; return stdout+stderr, never raise."""
    exe = shutil.which(cmd[0])
    if exe is None:
        return f"<{cmd[0]} not installed>"
    # S603: arguments are literals chosen by these scripts, not user or network input.
    proc = subprocess.run(  # noqa: S603
        [exe, *cmd[1:]], capture_output=True, text=True, timeout=20, check=False
    )
    return (proc.stdout + proc.stderr).strip()


def environment() -> dict[str, Any]:
    """The setup facts the spike report asks for (OS, desktop, versions)."""
    os_release: dict[str, str] = {}
    release = Path("/etc/os-release")
    if release.exists():
        for line in release.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            os_release[key] = value.strip('"')
    return {
        "os": os_release.get("PRETTY_NAME", platform.platform()),
        "kernel": platform.release(),
        "python": sys.version.split()[0],
        "session_type": os.environ.get("XDG_SESSION_TYPE"),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP"),
        "gnome_shell": run_text(["gnome-shell", "--version"]),
        "pipewire": run_text(["pipewire", "--version"]).splitlines()[-1:],
        "portal_pkgs": run_text(
            [
                "dpkg-query",
                "-W",
                "-f=${Package} ${Version}\\n",
                "xdg-desktop-portal",
                "xdg-desktop-portal-gnome",
                "xdg-desktop-portal-gtk",
                "pipewire",
                "wireplumber",
                "gstreamer1.0-pipewire",
            ]
        ).splitlines(),
        "cpu": _cpu_model(),
    }


def _cpu_model() -> str | None:
    cpuinfo = Path("/proc/cpuinfo")
    if not cpuinfo.exists():
        return None
    for line in cpuinfo.read_text(encoding="utf-8").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return None
