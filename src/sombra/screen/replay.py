"""Replay a folder of screenshots as a ``contracts.ScreenSource`` (tests and replay harness).

Owns ``DirectoryScreenSource``: it yields ``<unix_ms>.png`` files in timestamp order.
An optional sidecar ``<unix_ms>.json`` supplies ``{"app": ..., "window_title": ...}``;
without it both are ``None``. Files that do not match the pattern are ignored.
Supports the S1-S3 pipeline in tests; no OS capture here.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from sombra.contracts import Screenshot

_PNG_RE = re.compile(r"^(\d+)\.png$")


class ReplayExhaustedError(EOFError):
    """Raised by ``grab`` when every screenshot in the folder has been replayed."""


class DirectoryScreenSource:
    """Yield the PNGs of ``directory`` one per ``grab()``, oldest first.

    ``speed`` paces the replay by the gaps between timestamps (``1.0`` = real time,
    ``10.0`` = ten times faster); ``None`` returns every frame immediately. ``tz`` sets
    the timezone of ``Screenshot.ts`` (default: the local zone, as live capture uses).
    """

    def __init__(
        self,
        directory: Path,
        *,
        speed: float | None = None,
        tz: tzinfo | None = None,
    ) -> None:
        if speed is not None and speed <= 0:
            raise ValueError("speed must be > 0")
        self.directory = directory
        self.speed = speed
        self.tz = tz
        files = []
        for p in directory.iterdir():
            if (m := _PNG_RE.match(p.name)) and p.is_file():
                files.append((int(m[1]), p))
        self._files = sorted(files)
        self._pos = 0
        self._closed = False

    def __len__(self) -> int:
        return len(self._files)

    async def grab(self) -> Screenshot:
        if self._closed:
            raise ReplayExhaustedError("source is closed")
        if self._pos >= len(self._files):
            raise ReplayExhaustedError(f"no more screenshots in {self.directory}")
        ms, path = self._files[self._pos]
        if self.speed is not None and self._pos > 0:
            gap_ms = ms - self._files[self._pos - 1][0]
            await asyncio.sleep(gap_ms / 1000 / self.speed)
        self._pos += 1
        app, title = _sidecar(path.with_suffix(".json"))
        ts = datetime.fromtimestamp(ms / 1000, tz=UTC)
        ts = ts.astimezone(self.tz) if self.tz is not None else ts.astimezone()
        return Screenshot(ts=ts, image=path.read_bytes(), app=app, window_title=title)

    async def close(self) -> None:
        self._closed = True


def _sidecar(path: Path) -> tuple[str | None, str | None]:
    if not path.is_file():
        return None, None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"sidecar must be a JSON object: {path}")
    app, title = data.get("app"), data.get("window_title")
    return (
        app if isinstance(app, str) else None,
        title if isinstance(title, str) else None,
    )
