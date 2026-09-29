"""Read a meeting folder into ``sombra.contracts`` records, tolerating what readers must.

Forward compatibility: unknown event types and unknown keys are ignored. A line that
cannot be parsed (for example a half-written last line after a crash) is skipped and
counted, never fatal. Nothing here writes to the folder.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from sombra.contracts import (
    ActionKind,
    ActionLogged,
    AgentErrorLogged,
    FrameRecord,
    LogEvent,
    SuggestionLogged,
    SummaryEpochLogged,
    TimelineEntry,
    TriggerLogged,
    Usage,
    parse_line,
)

LOG_FILE = "log.jsonl"
FRAME_INDEX = "frames/index.jsonl"
TRANSCRIPT = "transcript.md"
MEETING_TOML = "meeting.toml"

_EVENT_TYPES: dict[str, type[Any]] = {
    cls.TYPE: cls
    for cls in (TriggerLogged, SuggestionLogged, ActionLogged, AgentErrorLogged, SummaryEpochLogged)
}
_DATETIME_FIELDS = {"ts", "detected_at"}
_INT_FIELDS = {"latency_ms", "epoch", "width", "height"}


class _UnknownEvent(Exception):
    pass


@dataclass(frozen=True, slots=True)
class MeetingLog:
    events: list[LogEvent]
    unknown: int  # event types (or action kinds) this version does not know; ignored
    malformed: int  # lines that could not be parsed; skipped


def aware(dt: datetime) -> datetime:
    """Naive timestamps are taken as local time, so they compare with aware ones."""
    return dt if dt.tzinfo is not None else dt.astimezone()


def read_log(meeting_dir: Path) -> MeetingLog:
    events: list[LogEvent] = []
    unknown = malformed = 0
    for obj in _json_lines(meeting_dir / LOG_FILE):
        if obj is None:
            malformed += 1
            continue
        try:
            events.append(parse_event(obj))
        except _UnknownEvent:
            unknown += 1
        except (KeyError, TypeError, ValueError):
            malformed += 1
    return MeetingLog(events, unknown, malformed)


def parse_event(obj: dict[str, Any]) -> LogEvent:
    """Build the contract record for one ``log.jsonl`` object, ignoring unknown keys."""
    kind = obj.get("type")
    cls = _EVENT_TYPES.get(kind) if isinstance(kind, str) else None
    if cls is None:
        raise _UnknownEvent(kind)
    if cls is ActionLogged and obj.get("kind") not in set(ActionKind):
        raise _UnknownEvent(obj.get("kind"))
    event: LogEvent = cls(**_fields_of(cls, obj))
    return event


def read_frames(meeting_dir: Path) -> tuple[list[FrameRecord], int]:
    frames: list[FrameRecord] = []
    malformed = 0
    for obj in _json_lines(meeting_dir / FRAME_INDEX):
        try:
            if obj is None:
                raise ValueError("bad json")
            frames.append(FrameRecord(**_fields_of(FrameRecord, obj)))
        except (KeyError, TypeError, ValueError):
            malformed += 1
    return frames, malformed


def read_transcript(meeting_dir: Path, day: datetime) -> list[TimelineEntry]:
    """Timeline entries; the header block and any non-timeline line are skipped."""
    path = meeting_dir / TRANSCRIPT
    if not path.is_file():
        return []
    entries: list[TimelineEntry] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(parse_line(line, day=day))
            except ValueError:
                continue
    return entries


def meeting_day(meeting_dir: Path, known: Iterable[datetime]) -> datetime:
    """Date + tz for the transcript's ``HH:MM:SS``: ``started_at`` in meeting.toml, else the
    earliest logged timestamp, else now."""
    started = _started_at(meeting_dir / MEETING_TOML)
    if started is not None:
        return aware(started)
    known = [aware(t) for t in known]
    return min(known) if known else datetime.now().astimezone()


def _started_at(path: Path) -> datetime | None:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8")).get("started_at")
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _json_lines(path: Path) -> Iterable[dict[str, Any] | None]:
    """Each non-blank line as a dict, or None when it is not a JSON object."""
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                yield None
                continue
            yield obj if isinstance(obj, dict) else None


def _fields_of(cls: type[Any], obj: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in obj:
            continue  # missing required fields make the constructor raise TypeError
        value = obj[f.name]
        if f.name in _DATETIME_FIELDS:
            value = datetime.fromisoformat(value)
        elif f.name in _INT_FIELDS:
            value = _int(value)
        elif f.name == "usage":
            value = _usage(value)
        elif f.name == "kind":
            value = ActionKind(value)
        elif f.name == "frames_sent" and not isinstance(value, list):
            raise ValueError("frames_sent must be a list")
        out[f.name] = value
    return out


def _usage(value: Any) -> Usage:
    if not isinstance(value, dict):
        raise ValueError("usage must be an object")
    names = {f.name for f in fields(Usage)}
    return Usage(**{k: _int(v) for k, v in value.items() if k in names})


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"expected an integer, got {value!r}")
    return value
