"""``ScreenSource`` and ``FramePipeline`` fakes."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sombra.contracts import FrameRecord, Screenshot, format_frame_id

ShotFactory = Callable[[int], Screenshot]


def _default_shot(n: int) -> Screenshot:
    # A new "slide" every other grab, so the dedupe keeps about half.
    return Screenshot(
        ts=datetime.now().astimezone(),
        image=f"slide-{n // 2}".encode(),
        app="Zoom",
        window_title=f"Zoom - slide {n // 2}",
    )


class FakeScreenSource:
    """``grab()`` returns ``shots(n)`` for n = 0, 1, ...; raises on grabs in ``fail_on``."""

    def __init__(self, shots: ShotFactory = _default_shot, *, fail_on: set[int] | None = None):
        self.shots = shots
        self.fail_on = set(fail_on or ())
        self.grabs = 0
        self.closed = False

    async def grab(self) -> Screenshot:
        n = self.grabs
        self.grabs += 1
        if n in self.fail_on:
            raise RuntimeError(f"fake screen grab {n} failed")
        return self.shots(n)

    async def close(self) -> None:
        self.closed = True


class FakeFramePipeline:
    """Keeps a shot when its image bytes differ from the last kept one.

    Ids are ``f0001``, ``f0002``, ... With ``meeting_dir`` set it writes the
    image to ``frames/<id>.jpg`` so paths resolve like the real pipeline's.
    """

    def __init__(self, meeting_dir: Path | None = None) -> None:
        self.meeting_dir = meeting_dir
        self.processed: list[Screenshot] = []
        self.kept: list[FrameRecord] = []
        self._last: bytes | None = None

    def process(self, shot: Screenshot) -> FrameRecord | None:
        self.processed.append(shot)
        if shot.image == self._last:
            return None
        self._last = shot.image
        frame_id = format_frame_id(len(self.kept) + 1)
        rel = f"frames/{frame_id}.jpg"
        if self.meeting_dir is not None:
            path = self.meeting_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(shot.image)
        record = FrameRecord(
            id=frame_id,
            ts=shot.ts,
            path=rel,
            width=1280,
            height=720,
            app=shot.app,
            window_title=shot.window_title,
            diff_score=64.0 if not self.kept else 32.0,
            phash=hashlib.sha256(shot.image).hexdigest()[:16],
        )
        self.kept.append(record)
        return record
