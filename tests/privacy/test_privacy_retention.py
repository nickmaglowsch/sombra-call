import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sombra.privacy.retention import (
    RetentionPolicy,
    UnsafePathError,
    delete_meeting,
    resolve_meeting,
    sweep,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def touch(path: Path, age_days: float, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    ts = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(path, (ts, ts))
    return path


def make_meeting(root: Path, name: str, frame_ages: list[float], text_age: float) -> Path:
    m = root / name
    (m / "context").mkdir(parents=True)
    touch(m / "context" / "doc.md", text_age)
    touch(m / "meeting.toml", text_age)
    for i, age in enumerate(frame_ages, 1):
        touch(m / "frames" / f"f{i:04d}.jpg", age)
    touch(m / "frames" / "index.jsonl", text_age)
    for f in ("transcript.md", "summary.md", "log.jsonl"):
        touch(m / f, text_age)
    return m


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "meetings"
    r.mkdir()
    return r


def test_deletes_exactly_expired_frames(root: Path) -> None:
    m = make_meeting(root, "2026-09-20_1000_a", [8, 7, 6.9, 1], text_age=8)
    report = sweep(root, NOW)
    assert sorted(report.deleted) == [m / "frames" / "f0001.jpg", m / "frames" / "f0002.jpg"]
    assert report.skipped == []
    left = sorted(p.name for p in (m / "frames").iterdir())
    assert left == ["f0003.jpg", "f0004.jpg", "index.jsonl"]
    for f in ("transcript.md", "summary.md", "log.jsonl", "meeting.toml"):
        assert (m / f).exists()


def test_deletes_expired_text_and_whole_meeting_when_nothing_left(root: Path) -> None:
    old = make_meeting(root, "2026-08-01_1000_old", [60, 45], text_age=31)
    keep = make_meeting(root, "2026-09-28_1000_new", [1], text_age=1)
    report = sweep(root, NOW)
    assert report.deleted == [old]
    assert not old.exists()
    assert keep.exists()
    assert (keep / "frames" / "f0001.jpg").exists()


def test_text_expired_but_frame_kept_keeps_folder(root: Path) -> None:
    m = make_meeting(root, "m", [1], text_age=31)
    report = sweep(root, NOW)
    assert sorted(report.deleted) == sorted(
        [m / "frames" / "index.jsonl", m / "transcript.md", m / "summary.md", m / "log.jsonl"]
    )
    assert (m / "frames" / "f0001.jpg").exists()
    assert (m / "meeting.toml").exists()


def test_custom_policy(root: Path) -> None:
    m = make_meeting(root, "m", [2, 0.5], text_age=3)
    report = sweep(root, NOW, RetentionPolicy(frames_days=1, text_days=5))
    assert report.deleted == [m / "frames" / "f0001.jpg"]


def test_dry_run_deletes_nothing(root: Path) -> None:
    m = make_meeting(root, "m", [60], text_age=60)
    report = sweep(root, NOW, dry_run=True)
    assert report.dry_run
    assert report.deleted == [m]
    assert (m / "frames" / "f0001.jpg").exists()
    assert (m / "transcript.md").exists()


def test_fresh_meeting_without_content_is_kept(root: Path) -> None:
    m = root / "fresh"
    (m / "context").mkdir(parents=True)
    touch(m / "meeting.toml", 90)
    assert sweep(root, NOW).deleted == []
    assert m.exists()


def test_loose_files_in_root_ignored(root: Path) -> None:
    touch(root / "notes.txt", 400)
    assert sweep(root, NOW).deleted == []
    assert (root / "notes.txt").exists()


def test_symlinks_never_followed(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    victim_meeting = make_meeting(outside, "victim", [60], text_age=60)
    victim_file = touch(outside / "secret.jpg", 60)

    # a symlinked meeting folder, a symlinked frames dir, symlinked files
    (root / "linked-meeting").symlink_to(victim_meeting, target_is_directory=True)
    m = make_meeting(root, "m", [60], text_age=60)
    (m / "frames" / "f0099.jpg").symlink_to(victim_file)
    m2 = root / "m2"
    m2.mkdir()
    (m2 / "frames").symlink_to(victim_meeting / "frames", target_is_directory=True)
    (m2 / "transcript.md").symlink_to(victim_meeting / "transcript.md")

    report = sweep(root, NOW)

    # everything outside root is untouched
    assert victim_file.exists()
    assert (victim_meeting / "frames" / "f0001.jpg").exists()
    assert (victim_meeting / "transcript.md").exists()
    assert set(report.skipped) >= {
        root / "linked-meeting",
        m / "frames" / "f0099.jpg",
        m2 / "frames",
        m2 / "transcript.md",
    }
    # the symlink kept m's folder alive; its real expired files are gone
    assert (m / "frames" / "f0099.jpg").is_symlink()
    assert not (m / "frames" / "f0001.jpg").exists()
    assert not (m / "transcript.md").exists()
    assert m2.exists()


def test_symlinked_root_refused(root: Path, tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        sweep(link, NOW)


@pytest.mark.parametrize("bad", ["/", "~"])
def test_dangerous_roots_refused(bad: str) -> None:
    with pytest.raises(UnsafePathError):
        sweep(Path(bad).expanduser(), NOW)


def test_root_must_be_dir(tmp_path: Path) -> None:
    f = touch(tmp_path / "file", 0)
    with pytest.raises(UnsafePathError):
        sweep(f, NOW)
    with pytest.raises(FileNotFoundError):
        sweep(tmp_path / "missing", NOW)


def test_naive_now_rejected(root: Path) -> None:
    with pytest.raises(ValueError):
        sweep(root, datetime(2026, 9, 29))


def test_negative_days_rejected() -> None:
    with pytest.raises(ValueError):
        RetentionPolicy(frames_days=-1)


# --- delete --------------------------------------------------------------------------


def test_delete_requires_confirmation(root: Path) -> None:
    m = make_meeting(root, "m", [1], text_age=1)
    with pytest.raises(PermissionError):
        delete_meeting(root, "m", confirmed=False)
    assert m.exists()
    assert delete_meeting(root, "m", confirmed=True) == m
    assert not m.exists()


def test_delete_by_absolute_path(root: Path) -> None:
    m = make_meeting(root, "m", [1], text_age=1)
    delete_meeting(root, m, confirmed=True)
    assert not m.exists()


def test_delete_removes_inner_symlinks_without_following(root: Path, tmp_path: Path) -> None:
    outside = touch(tmp_path / "keep.txt", 0)
    outside_dir = tmp_path / "keepdir"
    touch(outside_dir / "a.txt", 0)
    m = make_meeting(root, "m", [1], text_age=1)
    (m / "context" / "link.txt").symlink_to(outside)
    (m / "context" / "linkdir").symlink_to(outside_dir, target_is_directory=True)
    delete_meeting(root, "m", confirmed=True)
    assert not m.exists()
    assert outside.exists()
    assert (outside_dir / "a.txt").exists()


@pytest.mark.parametrize("name", ["..", ".", "", "../meetings", "m/context", "missing"])
def test_resolve_refuses_non_meetings(root: Path, name: str) -> None:
    make_meeting(root, "m", [1], text_age=1)
    with pytest.raises(UnsafePathError):
        resolve_meeting(root, name)


def test_resolve_refuses_outside_root_and_symlinks(root: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    make_meeting(other, "x", [1], text_age=1)
    (root / "link").symlink_to(other / "x", target_is_directory=True)
    with pytest.raises(UnsafePathError):
        resolve_meeting(root, other / "x")
    with pytest.raises(UnsafePathError):
        resolve_meeting(root, "link")
    with pytest.raises(UnsafePathError):
        delete_meeting(root, "link", confirmed=True)
    assert (other / "x" / "transcript.md").exists()
