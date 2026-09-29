"""``TimelineStore`` fake that keeps everything in memory (append-only, like the real one)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sombra.contracts import FrameRecord, LogEvent, TimelineEntry, log_event_type, to_json_line


class InMemoryStore:
    def __init__(self, meeting_dir: Path) -> None:
        self._meeting_dir = meeting_dir
        self.entries: list[TimelineEntry] = []
        self.frames: list[FrameRecord] = []
        self.events: list[LogEvent] = []

    @property
    def meeting_dir(self) -> Path:
        return self._meeting_dir

    def append_entry(self, entry: TimelineEntry) -> None:
        self.entries.append(entry)

    def append_frame(self, record: FrameRecord) -> None:
        self.frames.append(record)

    def log(self, event: LogEvent) -> None:
        self.events.append(event)

    def entries_since(self, since: datetime) -> list[TimelineEntry]:
        return [e for e in self.entries if e.ts >= since]

    # --- test helpers ------------------------------------------------------------------

    def transcript(self) -> list[str]:
        """``transcript.md`` lines, as the real store would write them."""
        return [e.to_line() for e in self.entries]

    def log_lines(self) -> list[str]:
        """``log.jsonl`` lines, as the real store would write them."""
        return [to_json_line(e) for e in self.events]

    def log_types(self) -> list[str]:
        return [log_event_type(line) for line in self.log_lines()]
