"""Retention sweep and meeting deletion.

``sweep(root, now)`` walks every meeting folder directly under ``root`` and deletes:

- frame images (``frames/*`` except ``index.jsonl``) older than ``frames_days`` (7);
- ``transcript.md``, ``summary.md``, ``log.jsonl`` and ``frames/index.jsonl`` older
  than ``text_days`` (30);
- the rest of the meeting folder (``meeting.toml``, ``context/``, ``consent.json``)
  when this sweep removed the last of its recorded content above. A meeting that never
  recorded anything is left alone.

Age is the file's own mtime (the last append). Deletion is confined to ``root``:
symlinks are never followed (a symlinked meeting folder, ``frames/`` directory or
file is skipped and reported, never deleted through), and every path is checked to
resolve inside ``root`` before it is unlinked.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

FRAMES_DIR = "frames"
FRAME_INDEX = "index.jsonl"
TEXT_FILES = ("transcript.md", "summary.md", "log.jsonl")


class UnsafePathError(ValueError):
    """A path that is not a real meeting folder directly under the meetings root."""


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    frames_days: int = 7
    text_days: int = 30

    def __post_init__(self) -> None:
        if self.frames_days < 0 or self.text_days < 0:
            raise ValueError("retention days must be >= 0")


@dataclass(slots=True)
class SweepReport:
    deleted: list[Path] = field(default_factory=list)  # files, or whole meeting folders
    skipped: list[Path] = field(default_factory=list)  # symlinks / non-regular entries left alone
    dry_run: bool = False


def _is_inside(path: Path, root: Path) -> bool:
    """True if ``path`` (not resolved through its last component) lives under ``root``."""
    parent = path.parent.resolve(strict=True)
    return parent == root or root in parent.parents


def _real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _real_file(path: Path) -> os.stat_result | None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    return st if stat.S_ISREG(st.st_mode) else None


def _expired(st: os.stat_result, now: datetime, days: int) -> bool:
    mtime = datetime.fromtimestamp(st.st_mtime, tz=now.tzinfo)
    return now - mtime >= timedelta(days=days)


def _remove_file(path: Path, root: Path, report: SweepReport) -> None:
    if not _is_inside(path, root):
        report.skipped.append(path)
        return
    if not report.dry_run:
        path.unlink()
    report.deleted.append(path)


def _check_root(root: Path) -> Path:
    if root.is_symlink():
        # Following a symlinked root would sweep wherever it points; make the user be explicit.
        raise UnsafePathError(f"meetings root {root} is a symlink; pass its real path")
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise UnsafePathError(f"meetings root {root} is not a directory")
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise UnsafePathError(f"refusing to treat {resolved} as the meetings root")
    return resolved


def sweep(
    root: Path,
    now: datetime,
    policy: RetentionPolicy | None = None,
    *,
    dry_run: bool = False,
) -> SweepReport:
    """Delete expired meeting content under ``root``. ``now`` must be timezone-aware."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    policy = policy or RetentionPolicy()
    root = _check_root(root)
    report = SweepReport(dry_run=dry_run)

    for meeting in sorted(root.iterdir()):
        if not _real_dir(meeting):
            if meeting.is_symlink():
                report.skipped.append(meeting)
            continue
        _sweep_meeting(meeting, root, now, policy, report)
    return report


def _sweep_meeting(
    meeting: Path, root: Path, now: datetime, policy: RetentionPolicy, report: SweepReport
) -> None:
    remaining = 0  # recorded content left after this sweep
    frames = meeting / FRAMES_DIR

    if _real_dir(frames):
        for entry in sorted(frames.iterdir()):
            st = _real_file(entry)
            if st is None:
                if entry.is_symlink():
                    report.skipped.append(entry)
                if entry.exists() or entry.is_symlink():
                    remaining += 1  # symlinks and unexpected subfolders keep the meeting
                continue
            days = policy.text_days if entry.name == FRAME_INDEX else policy.frames_days
            if _expired(st, now, days):
                _remove_file(entry, root, report)
            else:
                remaining += 1
    elif frames.is_symlink():
        report.skipped.append(frames)
        remaining += 1  # we can't tell what is behind it, so keep the meeting

    for name in TEXT_FILES:
        path = meeting / name
        st = _real_file(path)
        if st is None:
            if path.is_symlink():
                report.skipped.append(path)
                remaining += 1
            continue
        if _expired(st, now, policy.text_days):
            _remove_file(path, root, report)
        else:
            remaining += 1

    if remaining == 0 and _had_content(meeting, report):
        _rmtree_meeting(meeting, root, report)


def _had_content(meeting: Path, report: SweepReport) -> bool:
    """True if this sweep deleted content from ``meeting``.

    A freshly created meeting (just ``meeting.toml`` + ``context/``) is never swept away.
    """
    return any(meeting in p.parents for p in report.deleted)


def _rmtree_meeting(meeting: Path, root: Path, report: SweepReport) -> None:
    if meeting.is_symlink() or meeting.parent.resolve() != root:
        report.skipped.append(meeting)
        return
    if not report.dry_run:
        _safe_rmtree(meeting)
    report.deleted = [p for p in report.deleted if meeting not in p.parents]
    report.deleted.append(meeting)


def _safe_rmtree(path: Path) -> None:
    # shutil.rmtree unlinks symlinks found inside the tree without following them; on
    # Linux and macOS it uses the fd-based implementation that also resists races.
    if not shutil.rmtree.avoids_symlink_attacks:  # pragma: no cover - not on our platforms
        raise UnsafePathError("this platform's rmtree can follow symlinks; refusing to delete")
    shutil.rmtree(path)


def resolve_meeting(root: Path, meeting: str | Path) -> Path:
    """``meeting`` (a name or path) as a real directory directly inside ``root``."""
    root = _check_root(root)
    candidate = Path(meeting)
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.name in ("", ".", ".."):
        raise UnsafePathError(f"not a meeting folder: {meeting}")
    if candidate.is_symlink():
        raise UnsafePathError(f"{candidate} is a symlink; refusing to delete through it")
    if not _real_dir(candidate):
        raise UnsafePathError(f"no meeting folder {candidate}")
    if candidate.parent.resolve(strict=True) != root:
        raise UnsafePathError(f"{candidate} is not directly inside the meetings root {root}")
    return root / candidate.name


def delete_meeting(root: Path, meeting: str | Path, *, confirmed: bool) -> Path:
    """Remove one whole meeting folder. ``confirmed`` must be True (the CLI asks first)."""
    path = resolve_meeting(root, meeting)
    if not confirmed:
        raise PermissionError(f"deleting {path} needs explicit confirmation")
    _safe_rmtree(path)
    return path
