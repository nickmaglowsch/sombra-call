"""``MeetingStore``: the single append-only writer of a meeting folder (T3, S6).

Every write is one ``os.write`` of a whole line to a file opened with ``O_APPEND``,
under one lock, so audio, screen and trigger paths can write concurrently from
threads or asyncio tasks without interleaving. Nothing is ever seeked, truncated
or rewritten. Lines reach the OS immediately (a process crash loses nothing); a
background thread ``fsync``s dirty files every ``fsync_interval`` seconds (default
1 s), so an OS crash or power loss loses at most about that much.
"""

from __future__ import annotations

import os
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

from sombra.contracts import (
    FrameMarker,
    FrameRecord,
    LogEvent,
    TimelineEntry,
    parse_line,
    to_json_line,
)
from sombra.store.meeting import FRAME_INDEX_FILE, LOG_FILE, TRANSCRIPT_FILE, read_started_at

# A timestamp this much earlier than the previous line means the meeting crossed midnight.
_ROLLOVER = timedelta(hours=12)


class MeetingStore:
    """Implements ``contracts.TimelineStore`` for one meeting folder.

    ``started_at`` supplies the date and timezone for the ``HH:MM:SS`` times in
    ``transcript.md``; it defaults to the value in ``meeting.toml``.
    """

    def __init__(
        self,
        meeting_dir: Path,
        *,
        started_at: datetime | None = None,
        fsync_interval: float = 1.0,
    ) -> None:
        if fsync_interval <= 0:
            raise ValueError("fsync_interval must be > 0")
        self._dir = Path(meeting_dir)
        self._day = started_at if started_at is not None else read_started_at(self._dir)
        self._lock = threading.Lock()
        self._transcript = _open_append(self._dir / TRANSCRIPT_FILE)
        self._index = _open_append(self._dir / FRAME_INDEX_FILE)
        self._log = _open_append(self._dir / LOG_FILE)
        self._dirty: set[BinaryIO] = set()
        self._closed = False
        self._interval = fsync_interval
        self._stop = threading.Event()
        self._syncer = threading.Thread(
            target=self._sync_loop, name=f"sombra-fsync:{self._dir.name}", daemon=True
        )
        self._syncer.start()

    # --- TimelineStore -------------------------------------------------------------

    @property
    def meeting_dir(self) -> Path:
        return self._dir

    def append_entry(self, entry: TimelineEntry) -> None:
        line = replace(entry, ts=_align_tz(entry.ts, self._day)).to_line()
        with self._lock:
            self._write(self._transcript, line)

    def append_frame(self, record: FrameRecord) -> None:
        """Write the index record, then its ``TELA`` marker (same id and timestamp).

        The index goes first so a marker never points at a frame missing from the index.
        """
        marker = FrameMarker(
            ts=_align_tz(record.ts, self._day),
            frame_id=record.id,
            window_title=record.window_title or record.app or "",
        )
        index_line, marker_line = to_json_line(record), marker.to_line()
        with self._lock:
            self._write(self._index, index_line)
            self._write(self._transcript, marker_line)

    def log(self, event: LogEvent) -> None:
        line = to_json_line(event)
        with self._lock:
            self._write(self._log, line)

    def entries_since(self, since: datetime) -> list[TimelineEntry]:
        """Timeline entries with ``ts >= since``, oldest first.

        Lines that are not timeline entries (the optional header, blank lines) are
        skipped. A partially written last line (a crash mid-write) is returned if it
        still parses, else skipped; it never raises.
        """
        with self._lock:
            data = (self._dir / TRANSCRIPT_FILE).read_bytes()
        since = _align_tz(since, self._day)
        entries: list[TimelineEntry] = []
        day = self._day
        prev: datetime | None = None
        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            try:
                entry = parse_line(raw.decode("utf-8", errors="replace"), day=day)
            except ValueError:
                continue
            if prev is not None and prev - entry.ts > _ROLLOVER:
                day += timedelta(days=1)
                entry = parse_line(raw.decode("utf-8", errors="replace"), day=day)
            prev = entry.ts
            if entry.ts >= since:
                entries.append(entry)
        return entries

    # --- lifecycle -----------------------------------------------------------------

    def sync(self) -> None:
        """``fsync`` every file written since the last sync.

        The fsync runs outside the write lock so a slow disk never stalls the
        audio/screen writers; lines written meanwhile are picked up next round.
        """
        with self._lock:
            if self._closed:
                return
            dirty = list(self._dirty)
            self._dirty.clear()
        for f in dirty:
            os.fsync(f.fileno())

    def close(self) -> None:
        """Sync and close. Safe to call twice."""
        if self._closed:
            return
        self._stop.set()
        self._syncer.join()
        with self._lock:
            for f in self._dirty:
                os.fsync(f.fileno())
            self._dirty.clear()
            self._closed = True
            for f in (self._transcript, self._index, self._log):
                f.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- internals -----------------------------------------------------------------

    def _write(self, f: BinaryIO, line: str) -> None:
        if self._closed:
            raise RuntimeError("MeetingStore is closed")
        f.write(line.encode("utf-8") + b"\n")  # unbuffered: one O_APPEND write per line
        self._dirty.add(f)

    def _sync_loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.sync()


def _open_append(path: Path) -> BinaryIO:
    """Open for append, unbuffered. If a crash left the file without a final newline,
    terminate that line first so the next append does not glue onto it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f: BinaryIO = path.open("ab", buffering=0)
    if path.stat().st_size > 0:
        with path.open("rb") as r:
            r.seek(-1, os.SEEK_END)
            if r.read(1) != b"\n":
                f.write(b"\n")
    return f


def _align_tz(ts: datetime, day: datetime) -> datetime:
    """Express ``ts`` in the meeting's timezone, so ``HH:MM:SS`` means one wall clock.

    Naive times are taken as meeting-local; aware times are converted.
    """
    if day.tzinfo is None:
        return ts if ts.tzinfo is None else ts.astimezone().replace(tzinfo=None)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=day.tzinfo)
    return ts.astimezone(day.tzinfo)
