"""DedupeFramePipeline: dHash dedupe, resize/encode, frame ids (PRD S2, S3; issue #5)."""

from __future__ import annotations

import io
import json
import random
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, UnidentifiedImageError

from sombra.contracts import FramePipeline, FrameRecord, Screenshot, to_json_line
from sombra.screen.pipeline import (
    DEFAULT_THRESHOLD,
    DedupeFramePipeline,
    dhash,
    format_hash,
    hamming,
)

T0 = datetime(2026, 9, 29, 14, 30, tzinfo=UTC)
TITLE = "Zoom - Roadmap Q4"


# --- synthetic screens ----------------------------------------------------------------


def slide(n: int, w: int = 320, h: int = 200) -> Image.Image:
    """A synthetic slide: title bar, one of a few layouts, bullet lines.

    Layouts mirror common deck slides (full-bleed picture, chart left/right, text only,
    section divider) so consecutive slides differ the way real slide changes do.
    """
    r = random.Random(n)
    layout = r.randrange(5)
    dark = layout == 4  # section divider
    img = Image.new("RGB", (w, h), (30, 30, 50) if dark else (250, 250, 250))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w, h // 8), fill=tuple(r.randrange(0, 120) for _ in range(3)))
    color = tuple(r.randrange(20, 200) for _ in range(3))
    if layout == 0:  # full-bleed picture
        d.rectangle((w // 16, h // 6, w - w // 16, h - h // 12), fill=color)
    elif layout in (1, 2):  # chart on the left / right, bullets on the other side
        x0 = w // 16 if layout == 1 else w // 2
        for i in range(5):
            bar = r.randrange(h // 8, h // 2)
            bx = x0 + i * w // 12
            d.rectangle((bx, h - h // 12 - bar, bx + w // 20, h - h // 12), fill=color)
    text_x = w // 2 + w // 16 if layout == 1 else w // 16
    text_w = w // 3 if layout in (1, 2) else w * 3 // 4
    if layout != 0:
        for i in range(r.randrange(3, 8)):
            y = h // 5 + i * h // 10
            fill = (220,) * 3 if dark else (40,) * 3
            d.rectangle(
                (text_x, y, text_x + r.randrange(text_w // 3, text_w), y + h // 60), fill=fill
            )
    return img


def with_clock(img: Image.Image, second: int) -> Image.Image:
    """Menu-bar clock in the top-right corner, as it ticks between shots."""
    img = img.copy()
    w = img.width
    ImageDraw.Draw(img).text((w - w // 6, 2), f"14:{second // 60:02d}:{second % 60:02d}")
    return img


def with_cursor(img: Image.Image) -> Image.Image:
    """A blinking text cursor: a thin vertical bar in the middle of the screen."""
    img = img.copy()
    w, h = img.size
    ImageDraw.Draw(img).rectangle((w // 3, h // 2, w // 3 + 1, h // 2 + h // 20), fill=(0,) * 3)
    return img


def with_camera_tile(img: Image.Image, seed: int, frac: float = 0.2) -> Image.Image:
    """A speaker's video tile in the bottom-right corner whose content changes every shot."""
    img = img.copy()
    r = random.Random(seed)
    d = ImageDraw.Draw(img)
    w, h = img.size
    tw, th = int(w * frac), int(h * frac)
    for _ in range(30):
        x, y = r.randrange(w - tw, w), r.randrange(h - th, h)
        d.rectangle(
            (x, y, x + r.randrange(3, 20), y + r.randrange(3, 20)),
            fill=tuple(r.randrange(256) for _ in range(3)),
        )
    return img


def ui_screenshot(w: int = 2560, h: int = 1600, seed: int = 3) -> Image.Image:
    """A 'typical' desktop at full resolution: flat UI chrome, panels and lines of text."""
    r = random.Random(seed)
    img = Image.new("RGB", (w, h), (245, 245, 245))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w, h // 20), fill=(40, 40, 60))
    d.rectangle((0, h // 20, w // 6, h), fill=(230, 232, 236))
    for _ in range(6):
        x0, y0 = r.randrange(w // 5, w * 2 // 3), r.randrange(h // 10, h * 2 // 3)
        x1, y1 = x0 + r.randrange(w // 8, w // 3), y0 + r.randrange(h // 8, h // 3)
        d.rectangle((x0, y0, x1, y1), fill=tuple(r.randrange(256) for _ in range(3)))
    for i in range(60):
        y, x = h // 10 + i * h // 70, w // 5 + r.randrange(0, w // 20)
        for _ in range(r.randrange(3, 12)):  # "words"
            ww = r.randrange(20, 120)
            d.rectangle((x, y, x + ww, y + 12), fill=(30, 30, 30))
            x += ww + 10
    # A photo-like region: smooth gradient plus noise, the hardest part for JPEG.
    px, py, pw, ph = w // 2, h // 2, w // 4, h // 4
    photo = Image.linear_gradient("L").resize((pw, ph)).convert("RGB")
    noise = Image.effect_noise((pw, ph), 40).convert("RGB")
    img.paste(Image.blend(photo, noise, 0.3), (px, py))
    return img


def png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def shot(
    img: Image.Image,
    ts: datetime = T0,
    title: str | None = TITLE,
    app: str | None = "zoom.us",
) -> Screenshot:
    return Screenshot(ts=ts, image=png(img), app=app, window_title=title)


# --- dHash ----------------------------------------------------------------------------


def test_dhash_is_64_bit_and_stable() -> None:
    a = dhash(slide(1))
    assert 0 <= a < 2**64
    assert a == dhash(slide(1))
    assert format_hash(a) == f"{a:016x}"
    assert len(format_hash(0)) == 16


def test_dhash_is_scale_invariant_enough() -> None:
    img = slide(2, 640, 400)
    assert hamming(dhash(img), dhash(img.resize((320, 200)))) <= 2


def test_hamming() -> None:
    assert hamming(0, 0) == 0
    assert hamming(0, 2**64 - 1) == 64
    assert hamming(0b1011, 0b0001) == 2


# --- dedupe (acceptance: identical / cursor / clock dropped, slide change kept) --------


def test_implements_frame_pipeline_port(tmp_path: Path) -> None:
    pipeline: FramePipeline = DedupeFramePipeline(tmp_path)
    assert pipeline.process(shot(slide(1))) is not None


def test_first_frame_is_kept(tmp_path: Path) -> None:
    rec = DedupeFramePipeline(tmp_path).process(shot(slide(1)))
    assert rec is not None
    assert rec.id == "f0001"
    assert rec.path == "frames/f0001.jpg"
    assert rec.diff_score == 64.0
    assert rec.phash == format_hash(dhash(slide(1)))
    assert (rec.app, rec.window_title, rec.ts) == ("zoom.us", TITLE, T0)


def test_identical_screenshot_is_dropped(tmp_path: Path) -> None:
    p = DedupeFramePipeline(tmp_path)
    assert p.process(shot(slide(1))) is not None
    assert p.process(shot(slide(1), T0 + timedelta(seconds=5))) is None
    assert [f.name for f in (tmp_path / "frames").iterdir()] == ["f0001.jpg"]


@pytest.mark.parametrize(
    "noisy",
    [
        pytest.param(lambda img: with_cursor(img), id="blinking-cursor"),
        pytest.param(lambda img: with_clock(img, 37), id="clock"),
        pytest.param(lambda img: with_cursor(with_clock(img, 59)), id="cursor+clock"),
        pytest.param(lambda img: with_camera_tile(img, 7), id="camera-tile"),
    ],
)
def test_cosmetic_changes_are_dropped(tmp_path: Path, noisy: object) -> None:
    for n in range(10):
        p = DedupeFramePipeline(tmp_path / str(n))
        base = slide(n)
        assert p.process(shot(base)) is not None
        assert p.process(shot(noisy(base))) is None  # type: ignore[operator]


def test_slide_change_is_kept(tmp_path: Path) -> None:
    p = DedupeFramePipeline(tmp_path)
    first = p.process(shot(slide(1)))
    second = p.process(shot(slide(2)))
    assert first is not None and second is not None
    assert second.id == "f0002"
    assert second.diff_score == hamming(dhash(slide(1)), dhash(slide(2)))
    assert second.diff_score >= DEFAULT_THRESHOLD


def test_distance_is_measured_to_last_kept_frame(tmp_path: Path) -> None:
    """Slow drift must not hide a change: compare to the last *kept* frame, not the last shot."""
    p = DedupeFramePipeline(tmp_path, threshold=64)
    assert p.process(shot(slide(1))) is not None
    assert p.process(shot(slide(2))) is None  # below the (maximal) threshold
    rec = p.process(shot(slide(3), title="Other window"))
    assert rec is not None
    assert rec.diff_score == hamming(dhash(slide(1)), dhash(slide(3)))


def test_window_title_change_keeps_frame(tmp_path: Path) -> None:
    p = DedupeFramePipeline(tmp_path)
    assert p.process(shot(slide(1), title="Zoom")) is not None
    rec = p.process(shot(slide(1), title="Slack"))
    assert rec is not None
    assert rec.diff_score == 0.0
    assert p.process(shot(slide(1), title="Slack")) is None


def test_title_none_is_not_a_change(tmp_path: Path) -> None:
    """Wayland reports no title: identical frames with ``None`` titles are still dropped."""
    p = DedupeFramePipeline(tmp_path)
    assert p.process(shot(slide(1), title=None, app=None)) is not None
    assert p.process(shot(slide(1), title=None, app=None)) is None


def test_threshold_is_configurable(tmp_path: Path) -> None:
    d = hamming(dhash(slide(1)), dhash(slide(2)))
    strict = DedupeFramePipeline(tmp_path / "a", threshold=d + 1)
    loose = DedupeFramePipeline(tmp_path / "b", threshold=d)
    for p in (strict, loose):
        p.process(shot(slide(1)))
    assert strict.process(shot(slide(2))) is None
    assert loose.process(shot(slide(2))) is not None


@pytest.mark.parametrize("kwargs", [{"threshold": 0}, {"threshold": 65}, {"max_width": 0}])
def test_rejects_bad_settings(tmp_path: Path, kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        DedupeFramePipeline(tmp_path, **kwargs)  # type: ignore[arg-type]


def test_rejects_unknown_format(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="format"):
        DedupeFramePipeline(tmp_path, image_format="gif")  # type: ignore[arg-type]


# --- skip hook ------------------------------------------------------------------------


def test_skip_hook_drops_without_becoming_reference(tmp_path: Path) -> None:
    blocked = {"1Password"}
    p = DedupeFramePipeline(tmp_path, skip=lambda s: s.app in blocked)
    assert p.process(shot(slide(1), app="1Password")) is None
    assert not (tmp_path / "frames").exists()
    rec = p.process(shot(slide(1), app="zoom.us"))
    assert rec is not None and rec.id == "f0001"


def test_skip_hook_can_be_set_later_for_pause(tmp_path: Path) -> None:
    p = DedupeFramePipeline(tmp_path)
    paused = True
    p.skip = lambda _s: paused
    assert p.process(shot(slide(1))) is None
    paused = False
    assert p.process(shot(slide(1))) is not None


# --- 1 h synthetic slideshow (acceptance: 40 ± 5 of 720 shots) -----------------------


def test_one_hour_slideshow_keeps_about_one_frame_per_change(tmp_path: Path) -> None:
    changes = 40
    shots_per_hour = 3600 // 5
    rng = random.Random(42)
    change_at = sorted(rng.sample(range(1, shots_per_hour), changes - 1))
    p = DedupeFramePipeline(tmp_path)
    kept: list[FrameRecord] = []
    slide_no = 0
    slides = {0: slide(0)}
    for i in range(shots_per_hour):
        if change_at and i == change_at[0]:
            change_at.pop(0)
            slide_no += 1
            slides[slide_no] = slide(slide_no)
        img = with_clock(slides[slide_no], i * 5)
        if i % 2:
            img = with_cursor(img)
        img = with_camera_tile(img, i)
        rec = p.process(shot(img, T0 + timedelta(seconds=5 * i)))
        if rec is not None:
            kept.append(rec)
    assert slide_no + 1 == changes
    print(f"\n1 h slideshow: {len(kept)} frames kept for {changes} slide changes")  # noqa: T201 - measured number for the PR
    assert abs(len(kept) - changes) <= 5, len(kept)
    assert len(kept) <= 100  # PRD: <= 100 frames per hour
    assert [r.id for r in kept] == [f"f{n:04d}" for n in range(1, len(kept) + 1)]


# --- resize and encode (acceptance: <= 1280 px, JPEG, <= ~200 KB at 1440p) -----------


@pytest.mark.parametrize("size", [(2560, 1440), (2560, 1600), (2880, 1800), (1920, 1080)])
def test_saved_frame_is_resized_jpeg_under_200kb(tmp_path: Path, size: tuple[int, int]) -> None:
    rec = DedupeFramePipeline(tmp_path).process(shot(ui_screenshot(*size)))
    assert rec is not None
    path = tmp_path / rec.path
    assert path.stat().st_size <= 200 * 1024
    with Image.open(path) as saved:
        assert saved.format == "JPEG"
        assert saved.size == (rec.width, rec.height)
    assert rec.width == 1280
    assert rec.height == round(size[1] * 1280 / size[0])


def test_small_screens_are_not_upscaled(tmp_path: Path) -> None:
    rec = DedupeFramePipeline(tmp_path).process(shot(slide(1, 800, 500)))
    assert rec is not None
    assert (rec.width, rec.height) == (800, 500)


def test_max_width_is_configurable(tmp_path: Path) -> None:
    rec = DedupeFramePipeline(tmp_path, max_width=640).process(shot(ui_screenshot(1920, 1080)))
    assert rec is not None
    assert (rec.width, rec.height) == (640, 360)


def test_rgba_and_grayscale_inputs_are_saved_as_rgb_jpeg(tmp_path: Path) -> None:
    p = DedupeFramePipeline(tmp_path)
    for mode in ("RGBA", "L", "P"):
        rec = p.process(shot(slide(len(mode)).convert(mode), title=mode))
        assert rec is not None
        with Image.open(tmp_path / rec.path) as saved:
            assert saved.mode == "RGB"


def test_webp_option(tmp_path: Path) -> None:
    rec = DedupeFramePipeline(tmp_path, image_format="webp").process(shot(ui_screenshot()))
    assert rec is not None
    assert rec.path == "frames/f0001.webp"
    with Image.open(tmp_path / rec.path) as saved:
        assert saved.format == "WEBP"


def test_does_not_write_the_index(tmp_path: Path) -> None:
    p = DedupeFramePipeline(tmp_path)
    p.process(shot(slide(1)))
    assert not (tmp_path / "frames" / "index.jsonl").exists()


# --- frame ids resume (acceptance: restart on an existing meeting folder) --------------


def _store_append(meeting: Path, rec: FrameRecord) -> None:
    """What the store's ``append_frame`` does: one JSON line per kept frame."""
    with (meeting / "frames" / "index.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(to_json_line(rec) + "\n")


def test_ids_continue_after_restart(tmp_path: Path) -> None:
    first = DedupeFramePipeline(tmp_path)
    for n in range(3):
        rec = first.process(shot(slide(n)))
        assert rec is not None
        _store_append(tmp_path, rec)

    second = DedupeFramePipeline(tmp_path)
    assert second.next_frame_id == "f0004"
    # The dedupe reference resumes too: the last kept frame is not saved again.
    assert second.process(shot(slide(2))) is None
    rec = second.process(shot(slide(10)))
    assert rec is not None and rec.id == "f0004"
    assert sorted(f.name for f in (tmp_path / "frames").glob("*.jpg")) == [
        "f0001.jpg",
        "f0002.jpg",
        "f0003.jpg",
        "f0004.jpg",
    ]


def test_title_change_is_detected_across_restart(tmp_path: Path) -> None:
    rec = DedupeFramePipeline(tmp_path).process(shot(slide(1), title="Zoom"))
    assert rec is not None
    _store_append(tmp_path, rec)
    p = DedupeFramePipeline(tmp_path)
    assert p.process(shot(slide(1), title="Zoom")) is None
    assert p.process(shot(slide(1), title="Slack")) is not None


def test_resume_never_overwrites_unindexed_frame(tmp_path: Path) -> None:
    """A crash between saving the jpg and appending the index must not lose the file."""
    (tmp_path / "frames").mkdir()
    (tmp_path / "frames" / "f0007.jpg").write_bytes(b"kept before crash")
    p = DedupeFramePipeline(tmp_path)
    assert p.next_frame_id == "f0008"
    rec = p.process(shot(slide(1)))
    assert rec is not None and rec.id == "f0008"
    assert (tmp_path / "frames" / "f0007.jpg").read_bytes() == b"kept before crash"


def test_skips_ids_taken_after_start(tmp_path: Path) -> None:
    p = DedupeFramePipeline(tmp_path)
    (tmp_path / "frames").mkdir()
    (tmp_path / "frames" / "f0001.jpg").write_bytes(b"other writer")
    rec = p.process(shot(slide(1)))
    assert rec is not None and rec.id == "f0002"
    assert (tmp_path / "frames" / "f0001.jpg").read_bytes() == b"other writer"


def test_resume_tolerates_malformed_index_lines(tmp_path: Path) -> None:
    frames = tmp_path / "frames"
    frames.mkdir()
    lines = [
        json.dumps({"id": "f0002", "phash": "zz", "window_title": 5}),
        "not json",
        "",
        json.dumps({"no_id": 1}),
        json.dumps({"id": "f0001", "phash": format_hash(dhash(slide(1)))}),
        json.dumps(["list"]),
    ]
    (frames / "index.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    p = DedupeFramePipeline(tmp_path)
    assert p.next_frame_id == "f0003"
    rec = p.process(shot(slide(1)))  # bad phash on the last record: no reference, kept
    assert rec is not None and rec.id == "f0003"


def test_failed_write_leaves_no_partial_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    p = DedupeFramePipeline(tmp_path)
    s = shot(slide(1))
    monkeypatch.setattr(Image.Image, "save", boom)
    with pytest.raises(OSError, match="disk full"):
        p.process(s)
    assert list((tmp_path / "frames").iterdir()) == []
    monkeypatch.undo()
    rec = p.process(s)  # the failed shot did not become the dedupe reference
    assert rec is not None


def test_undecodable_image_raises(tmp_path: Path) -> None:
    bad = Screenshot(ts=T0, image=b"not a png", app=None, window_title=None)
    with pytest.raises(UnidentifiedImageError):
        DedupeFramePipeline(tmp_path).process(bad)


def test_empty_meeting_starts_at_one(tmp_path: Path) -> None:
    assert DedupeFramePipeline(tmp_path / "new").next_frame_id == "f0001"


# --- performance (acceptance: < 50 ms per 2560x1600 screenshot on CI) -----------------


def _median_ms(p: DedupeFramePipeline, screenshots: list[Screenshot]) -> float:
    times = []
    for s in screenshots:
        t0 = time.perf_counter()
        p.process(s)
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times)


def test_processing_a_2560x1600_screenshot_is_fast(tmp_path: Path) -> None:
    kept = [shot(ui_screenshot(seed=s), title=str(s)) for s in range(7)]
    dropped = [shot(ui_screenshot(seed=1))] * 7
    kept_ms = _median_ms(DedupeFramePipeline(tmp_path / "kept"), kept)
    p = DedupeFramePipeline(tmp_path / "dropped")
    p.process(dropped[0])
    dropped_ms = _median_ms(p, dropped)
    print(f"\n2560x1600 per shot: kept {kept_ms:.1f} ms, dropped {dropped_ms:.1f} ms (median)")  # noqa: T201 - measured number for the PR
    assert kept_ms < 50
    assert dropped_ms < 50
