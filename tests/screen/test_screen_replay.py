"""DirectoryScreenSource: replay a folder of ``<unix_ms>.png`` screenshots (issue #5)."""

from __future__ import annotations

import io
import json
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from sombra.contracts import ScreenSource
from sombra.screen.replay import DirectoryScreenSource, ReplayExhaustedError

MS = 1_790_000_000_000  # an arbitrary unix time in ms


def _png(color: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (color, color, color)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    (tmp_path / f"{MS + 10_000}.png").write_bytes(_png(3))
    (tmp_path / f"{MS}.png").write_bytes(_png(1))
    (tmp_path / f"{MS + 5_000}.png").write_bytes(_png(2))
    (tmp_path / f"{MS}.json").write_text(
        json.dumps({"app": "zoom.us", "window_title": "Zoom - Roadmap Q4"}), encoding="utf-8"
    )
    (tmp_path / f"{MS + 5_000}.json").write_text(json.dumps({"app": 3}), encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / "shot.png").write_bytes(_png(9))  # not <unix_ms>.png: ignored
    (tmp_path / "123.png").mkdir()  # a directory, not a file: ignored
    return tmp_path


async def test_replays_in_timestamp_order_with_sidecars(folder: Path) -> None:
    src: ScreenSource = DirectoryScreenSource(folder, tz=UTC)
    first, second, third = [await src.grab() for _ in range(3)]
    assert first.ts == datetime.fromtimestamp(MS / 1000, tz=UTC)
    assert second.ts - first.ts == timedelta(seconds=5)
    assert third.ts - first.ts == timedelta(seconds=10)
    assert first.image == _png(1) and second.image == _png(2) and third.image == _png(3)
    assert (first.app, first.window_title) == ("zoom.us", "Zoom - Roadmap Q4")
    assert (second.app, second.window_title) == (None, None)  # non-string values dropped
    assert (third.app, third.window_title) == (None, None)  # no sidecar


async def test_len_counts_only_screenshots(folder: Path) -> None:
    assert len(DirectoryScreenSource(folder)) == 3


async def test_exhausted_raises(folder: Path) -> None:
    src = DirectoryScreenSource(folder)
    for _ in range(3):
        await src.grab()
    with pytest.raises(ReplayExhaustedError):
        await src.grab()
    assert issubclass(ReplayExhaustedError, EOFError)


async def test_close_stops_grab(folder: Path) -> None:
    src = DirectoryScreenSource(folder)
    await src.close()
    with pytest.raises(ReplayExhaustedError, match="closed"):
        await src.grab()


async def test_timestamps_default_to_local_zone(folder: Path) -> None:
    ts = (await DirectoryScreenSource(folder).grab()).ts
    assert ts.tzinfo is not None
    assert ts == datetime.fromtimestamp(MS / 1000, tz=UTC)
    assert ts.utcoffset() == datetime.fromtimestamp(MS / 1000).astimezone().utcoffset()


async def test_custom_timezone(folder: Path) -> None:
    sp = timezone(timedelta(hours=-3))
    ts = (await DirectoryScreenSource(folder, tz=sp).grab()).ts
    assert ts.utcoffset() == timedelta(hours=-3)


async def test_speed_paces_by_timestamp_gaps(folder: Path) -> None:
    src = DirectoryScreenSource(folder, speed=100.0)  # 5 s gaps -> 50 ms
    await src.grab()
    t0 = time.perf_counter()
    await src.grab()
    assert time.perf_counter() - t0 >= 0.045


async def test_empty_folder(tmp_path: Path) -> None:
    with pytest.raises(ReplayExhaustedError):
        await DirectoryScreenSource(tmp_path).grab()


@pytest.mark.parametrize("speed", [0.0, -1.0])
def test_rejects_bad_speed(tmp_path: Path, speed: float) -> None:
    with pytest.raises(ValueError, match="speed"):
        DirectoryScreenSource(tmp_path, speed=speed)


async def test_sidecar_must_be_an_object(tmp_path: Path) -> None:
    (tmp_path / f"{MS}.png").write_bytes(_png(1))
    (tmp_path / f"{MS}.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        await DirectoryScreenSource(tmp_path).grab()
