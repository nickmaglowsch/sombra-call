"""Create a meeting folder from the template (M1) and write its ``meeting.toml``.

Layout (see docs/ARCHITECTURE.md)::

    2026-09-29_1430_daily-time-x/
      meeting.toml
      context/          copies of the profile's context files (M2)
      transcript.md     empty, or one header block written once
      frames/index.jsonl
      log.jsonl
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sombra.contracts import AutonomyLevel

MEETING_FILE = "meeting.toml"
TRANSCRIPT_FILE = "transcript.md"
FRAMES_DIR = "frames"
FRAME_INDEX_FILE = "frames/index.jsonl"
LOG_FILE = "log.jsonl"
CONTEXT_DIR = "context"

_MAX_SLUG = 60


def slugify(name: str) -> str:
    """ASCII, lowercase, dash-separated: ``"Daily - Time X (Ação)"`` -> ``"daily-time-x-acao"``."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    slug = slug[:_MAX_SLUG].rstrip("-")
    return slug or "meeting"


@dataclass(frozen=True, slots=True)
class MeetingInfo:
    """What goes into ``meeting.toml``. ``config.load_meeting_config`` reads it back."""

    name: str
    started_at: datetime
    aliases: Sequence[str] = ()
    autonomy_level: AutonomyLevel = AutonomyLevel.L1
    allowed_topics: Sequence[str] = ()
    profile: str | None = None
    context_files: Sequence[str] = field(default=())  # relative to the meeting dir

    def to_toml(self) -> str:
        lines = [
            f"name = {_toml_str(self.name)}",
            f"started_at = {self.started_at.isoformat(timespec='seconds')}",
            f"aliases = {_toml_list(self.aliases)}",
            f"autonomy_level = {_toml_str(self.autonomy_level.value)}",
            f"allowed_topics = {_toml_list(self.allowed_topics)}",
        ]
        if self.profile is not None:
            lines.append(f"profile = {_toml_str(self.profile)}")
        lines.append(f"context_files = {_toml_list(self.context_files)}")
        return "\n".join(lines) + "\n"


def _toml_str(s: str) -> str:
    # A JSON string is a valid TOML basic string (same escapes, \uXXXX for controls).
    return json.dumps(s, ensure_ascii=False)


def _toml_list(items: Sequence[str]) -> str:
    return "[" + ", ".join(_toml_str(s) for s in items) + "]"


def create_meeting(
    root: Path,
    name: str,
    profile: str | None = None,
    *,
    context_paths: Sequence[Path] = (),
    aliases: Sequence[str] = (),
    autonomy_level: AutonomyLevel = AutonomyLevel.L1,
    allowed_topics: Sequence[str] = (),
    started_at: datetime | None = None,
    header: str | None = None,
) -> Path:
    """Make ``root/YYYY-MM-DD_HHMM_<slug>/`` and return its path.

    ``context_paths`` (files or directories, usually the profile's) are copied into
    ``context/``. A missing context path raises ``FileNotFoundError`` before anything
    is created. ``header``, if given, is written once at the top of ``transcript.md``;
    after that the file is append-only.
    """
    if not name.strip():
        raise ValueError("meeting name must not be empty")
    started = started_at or datetime.now()
    if started.tzinfo is None:
        started = started.astimezone()  # local wall clock, with its UTC offset
    sources = [Path(p).expanduser() for p in context_paths]
    for src in sources:
        if not src.exists():
            raise FileNotFoundError(f"context path does not exist: {src}")

    root = Path(root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    meeting_dir = _unique_dir(root, f"{started:%Y-%m-%d_%H%M}_{slugify(name)}")
    (meeting_dir / CONTEXT_DIR).mkdir()
    (meeting_dir / FRAMES_DIR).mkdir()

    copied = _copy_context(sources, meeting_dir / CONTEXT_DIR)
    info = MeetingInfo(
        name=name,
        started_at=started,
        aliases=tuple(aliases),
        autonomy_level=autonomy_level,
        allowed_topics=tuple(allowed_topics),
        profile=profile,
        context_files=tuple(p.relative_to(meeting_dir).as_posix() for p in copied),
    )
    (meeting_dir / MEETING_FILE).write_text(info.to_toml(), encoding="utf-8")

    transcript = "" if header is None else header.rstrip("\n") + "\n\n"
    (meeting_dir / TRANSCRIPT_FILE).write_text(transcript, encoding="utf-8")
    (meeting_dir / FRAME_INDEX_FILE).touch()
    (meeting_dir / LOG_FILE).touch()
    return meeting_dir


def _unique_dir(root: Path, base: str) -> Path:
    """Create and return ``root/base``, or ``base-2``, ``base-3``... if it exists."""
    n = 1
    while True:
        candidate = root / (base if n == 1 else f"{base}-{n}")
        try:
            candidate.mkdir()
        except FileExistsError:
            n += 1
            continue
        return candidate


def _copy_context(sources: Sequence[Path], dest: Path) -> list[Path]:
    copied: list[Path] = []
    for src in sources:
        target = _unique_target(dest, src.name)
        if src.is_dir():
            shutil.copytree(src, target)
            copied.extend(sorted(p for p in target.rglob("*") if p.is_file()))
        else:
            shutil.copy2(src, target)
            copied.append(target)
    return copied


def _unique_target(dest: Path, name: str) -> Path:
    target = dest / name
    n = 2
    while target.exists():
        stem, suffix = Path(name).stem, Path(name).suffix
        target = dest / f"{stem}-{n}{suffix}"
        n += 1
    return target


def read_started_at(meeting_dir: Path) -> datetime:
    """``started_at`` from ``meeting.toml``; supplies the date for ``HH:MM:SS`` lines."""
    path = Path(meeting_dir) / MEETING_FILE
    with path.open("rb") as f:
        value = tomllib.load(f).get("started_at")
    if not isinstance(value, datetime):
        raise ValueError(f"{path}: started_at: expected a TOML datetime")
    return value
