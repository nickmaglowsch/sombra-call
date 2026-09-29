"""Frame pipeline: dHash dedupe, resize and save kept screenshots (PRD S2, S3; records for S6).

Owns turning a stream of full-resolution screenshots into a few distinct frames on
disk. Pure Python on Pillow, identical on macOS and Linux:

- a 64-bit **dHash** of each screenshot; a frame is kept when its Hamming distance to
  the last *kept* frame is at least ``threshold``, or when the window title changed;
- a kept frame is resized to at most ``max_width`` px wide and saved as
  ``frames/<id>.jpg`` (JPEG q80) or ``frames/<id>.webp``;
- frame ids come from ``contracts.format_frame_id`` with a per-meeting counter that
  resumes from an existing ``frames/index.jsonl`` (and never overwrites a saved frame).

The pipeline returns a ``FrameRecord`` but does not write ``index.jsonl``: the store
(``TimelineStore.append_frame``) is the single writer of the meeting folder index.

Known limit of a 64-bit dHash: two slides with the same layout that differ only in
their text can hash within ``threshold`` of each other and the second one is dropped.
Cosmetic noise (clock, blinking cursor, a speaker's camera tile) measured at most 5
bits in the tests, so the default threshold of 8 leaves a margin above it.

Imaging: Pillow (MIT-CMU / HPND licence, permissive).
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from PIL import Image

from sombra.contracts import FrameRecord, Screenshot, format_frame_id

log = logging.getLogger(__name__)

HASH_BITS = 64
DEFAULT_THRESHOLD = 8  # Hamming bits; cosmetic noise measured <= 5, see tests/screen
DEFAULT_MAX_WIDTH = 1280
DEFAULT_QUALITY = 80

FrameFormat = Literal["jpeg", "webp"]
_EXT: dict[FrameFormat, str] = {"jpeg": "jpg", "webp": "webp"}
_FRAME_FILE_RE = re.compile(r"^f(\d{4,})\.(?:jpg|webp)$")


def dhash(image: Image.Image) -> int:
    """64-bit difference hash: 9x8 grayscale thumbnail, one bit per horizontal gradient."""
    small = image.convert("L").resize((9, 8), Image.Resampling.BOX)
    px = small.tobytes()  # 72 bytes, row-major
    value = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            value = (value << 1) | (px[base + col] > px[base + col + 1])
    return value


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def format_hash(value: int) -> str:
    return f"{value:016x}"


class DedupeFramePipeline:
    """``contracts.FramePipeline`` that keeps only frames that changed (S2) and saves them (S3).

    ``skip`` is an optional hook the orchestrator uses for blocked apps and pause: a
    screenshot for which it returns ``True`` is dropped before any decoding and does
    not become the reference for later dedupe.
    """

    def __init__(
        self,
        meeting_dir: Path,
        *,
        threshold: int = DEFAULT_THRESHOLD,
        max_width: int = DEFAULT_MAX_WIDTH,
        image_format: FrameFormat = "jpeg",
        quality: int = DEFAULT_QUALITY,
        skip: Callable[[Screenshot], bool] | None = None,
    ) -> None:
        if not 1 <= threshold <= HASH_BITS:
            raise ValueError(f"threshold must be in 1..{HASH_BITS}")
        if max_width < 1:
            raise ValueError("max_width must be >= 1")
        if image_format not in _EXT:
            raise ValueError(f"unsupported image format: {image_format!r}")
        self.meeting_dir = meeting_dir
        self.frames_dir = meeting_dir / "frames"
        self.threshold = threshold
        self.max_width = max_width
        self.image_format: FrameFormat = image_format
        self.quality = quality
        self.skip = skip
        self._last_hash: int | None = None
        self._last_title: str | None = None
        self._counter = 0
        self._resume()

    @property
    def next_frame_id(self) -> str:
        return format_frame_id(self._counter + 1)

    def process(self, shot: Screenshot) -> FrameRecord | None:
        if self.skip is not None and self.skip(shot):
            return None
        with Image.open(io.BytesIO(shot.image)) as img:
            img.load()
            small = self._downscale(img)
        value = dhash(small)
        if self._last_hash is None:
            diff = HASH_BITS
        else:
            diff = hamming(value, self._last_hash)
            if diff < self.threshold and shot.window_title == self._last_title:
                return None
        record = self._save(small, shot, value, diff)
        self._last_hash = value
        self._last_title = shot.window_title
        return record

    def _downscale(self, img: Image.Image) -> Image.Image:
        """RGB, at most ``max_width`` wide. Integer box ``reduce`` first: it is the fast path."""
        out = img if img.mode == "RGB" else img.convert("RGB")
        if (factor := out.width // self.max_width) > 1:
            out = out.reduce(factor)
        if out.width > self.max_width:
            height = max(1, round(out.height * self.max_width / out.width))
            out = out.resize((self.max_width, height), Image.Resampling.BILINEAR)
        return out

    def _save(self, out: Image.Image, shot: Screenshot, value: int, diff: int) -> FrameRecord:
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        while True:
            self._counter += 1
            frame_id = format_frame_id(self._counter)
            rel = f"frames/{frame_id}.{_EXT[self.image_format]}"
            path = self.meeting_dir / rel
            try:
                with path.open("xb") as fh:  # never overwrite a saved frame
                    out.save(fh, format=self.image_format.upper(), quality=self.quality)
                break
            except FileExistsError:
                continue
        return FrameRecord(
            id=frame_id,
            ts=shot.ts,
            path=rel,
            width=out.width,
            height=out.height,
            app=shot.app,
            window_title=shot.window_title,
            diff_score=float(diff),
            phash=format_hash(value),
        )

    def _resume(self) -> None:
        """Continue the counter (and dedupe reference) from an existing meeting folder."""
        highest = 0
        if self.frames_dir.is_dir():
            for p in self.frames_dir.iterdir():
                if m := _FRAME_FILE_RE.match(p.name):
                    highest = max(highest, int(m[1]))
        index = self.frames_dir / "index.jsonl"
        last: dict[str, object] | None = None
        last_n = -1
        if index.is_file():
            with index.open(encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                        n = int(str(data["id"]).removeprefix("f"))
                    except (ValueError, KeyError, TypeError):
                        log.warning("ignoring malformed line in %s", index)
                        continue
                    if n >= last_n:
                        last, last_n = data, n
        self._counter = max(highest, last_n, 0)
        if last is not None:
            phash, title = last.get("phash"), last.get("window_title")
            try:
                self._last_hash = int(phash, 16) if isinstance(phash, str) else None
            except ValueError:
                self._last_hash = None
            self._last_title = title if isinstance(title, str) else None
