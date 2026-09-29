"""The single timeline: every line of ``transcript.md``.

Speech and screen markers share one append-only text format, so the agent can
``grep`` the whole meeting and the prompt prefix stays byte-stable between calls.

Canonical line formats (one entry per line, never rewritten)::

    [14:32:07] EU: acho que dá pra fechar na sexta
    [14:32:09] OUTROS: Nick, o que você acha desse gráfico?
    [14:32:09] OUTROS(2): concordo            <- optional speaker tag (T5 diarization)
    [14:32:10] TELA f0123 "Zoom - Roadmap Q4"

Times are local wall-clock ``HH:MM:SS``; the full timestamp lives in
``frames/index.jsonl`` and ``log.jsonl``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum

FRAME_ID_RE = re.compile(r"^f\d{4,}$")


class Channel(StrEnum):
    """Audio channel. Separate capture of mic vs. system audio gives "me vs. others" for free."""

    ME = "EU"
    OTHERS = "OUTROS"


def format_frame_id(n: int) -> str:
    """Frame ids are ``f`` + a zero-padded (min 4 digits) per-meeting counter: ``f0001``."""
    if n < 0:
        raise ValueError("frame counter must be >= 0")
    return f"f{n:04d}"


@dataclass(frozen=True, slots=True)
class SpeechLine:
    ts: datetime
    channel: Channel
    text: str
    speaker: str | None = None  # "2" for OUTROS(2); None when diarization is off

    def to_line(self) -> str:
        who = self.channel.value
        if self.speaker is not None:
            who = f"{who}({self.speaker})"
        text = " ".join(self.text.split())  # one entry per line, always
        return f"[{self.ts:%H:%M:%S}] {who}: {text}"


@dataclass(frozen=True, slots=True)
class FrameMarker:
    ts: datetime
    frame_id: str
    window_title: str

    def __post_init__(self) -> None:
        if not FRAME_ID_RE.match(self.frame_id):
            raise ValueError(f"invalid frame id: {self.frame_id!r}")

    def to_line(self) -> str:
        title = " ".join(self.window_title.split()).replace('"', "'")
        return f'[{self.ts:%H:%M:%S}] TELA {self.frame_id} "{title}"'


TimelineEntry = SpeechLine | FrameMarker

_SPEECH_RE = re.compile(
    r"^\[(?P<t>\d{2}:\d{2}:\d{2})\] (?P<ch>EU|OUTROS)(?:\((?P<spk>[^)]+)\))?: (?P<text>.*)$"
)
_FRAME_RE = re.compile(r'^\[(?P<t>\d{2}:\d{2}:\d{2})\] TELA (?P<id>f\d{4,}) "(?P<title>.*)"$')


def parse_line(line: str, *, day: datetime) -> TimelineEntry:
    """Parse one ``transcript.md`` line; ``day`` supplies the date and tzinfo for ``HH:MM:SS``."""
    line = line.rstrip("\n")
    if m := _FRAME_RE.match(line):
        return FrameMarker(ts=_at(day, m["t"]), frame_id=m["id"], window_title=m["title"])
    if m := _SPEECH_RE.match(line):
        return SpeechLine(
            ts=_at(day, m["t"]), channel=Channel(m["ch"]), text=m["text"], speaker=m["spk"]
        )
    raise ValueError(f"not a timeline line: {line!r}")


def _at(day: datetime, hms: str) -> datetime:
    return datetime.combine(day.date(), time.fromisoformat(hms), tzinfo=day.tzinfo)
