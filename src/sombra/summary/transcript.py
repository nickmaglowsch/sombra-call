"""Reading ``transcript.md`` and splitting long transcripts into model-sized chunks."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from sombra.contracts import TimelineEntry, parse_line

CHARS_PER_TOKEN = 3.5  # conservative for PT-BR with timestamps; no tokenizer dependency


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    """One canonical timeline line plus its parsed entry."""

    text: str
    entry: TimelineEntry

    @property
    def hms(self) -> str:
        return f"{self.entry.ts:%H:%M:%S}"


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def to_lines(entries: Sequence[TimelineEntry]) -> list[TranscriptLine]:
    return [TranscriptLine(e.to_line(), e) for e in entries]


def read_transcript(path: Path, *, day: datetime) -> list[TranscriptLine]:
    """Parse every timeline line; skip the header block and anything unparsable.

    Times that go backwards by more than 12 h are taken as crossing midnight, so the
    returned timestamps stay monotonic for meetings that run past 00:00.
    """
    out: list[TranscriptLine] = []
    offset = timedelta(0)
    prev: datetime | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = parse_line(raw, day=day)
        except ValueError:
            continue
        ts = entry.ts + offset
        if prev is not None and prev - ts > timedelta(hours=12):
            offset += timedelta(days=1)
            ts += timedelta(days=1)
        prev = ts
        entry = replace(entry, ts=ts)
        out.append(TranscriptLine(raw.rstrip("\n"), entry))
    return out


def chunk_lines(lines: Sequence[TranscriptLine], max_tokens: int) -> list[list[TranscriptLine]]:
    """Split into consecutive chunks of at most ``max_tokens`` (estimated) each.

    Lines are never split; a single line longer than the budget gets its own chunk.
    """
    if max_tokens <= 0:
        raise ValueError("max_tokens must be > 0")
    chunks: list[list[TranscriptLine]] = []
    current: list[TranscriptLine] = []
    used = 0
    for line in lines:
        cost = estimate_tokens(line.text + "\n")
        if current and used + cost > max_tokens:
            chunks.append(current)
            current, used = [], 0
        current.append(line)
        used += cost
    if current:
        chunks.append(current)
    return chunks


def render(lines: Sequence[TranscriptLine]) -> str:
    return "\n".join(line.text for line in lines)
