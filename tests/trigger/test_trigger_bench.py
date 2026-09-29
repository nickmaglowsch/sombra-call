"""``feed()`` must stay far below the 200 ms trigger budget: p99 < 5 ms on CI."""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sombra.contracts import TimelineEntry, parse_line
from sombra.trigger import NameTriggerDetector, load_corpus

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "trigger" / "corpus.jsonl"
DAY = datetime(2026, 9, 29, tzinfo=UTC)


def _meeting(n: int) -> list[TimelineEntry]:
    """A long meeting: every corpus line replayed in a loop, one line every ~2 s."""
    lines = [ln for case in load_corpus(CORPUS) for ln in case.lines]
    entries: list[TimelineEntry] = []
    for i in range(n):
        entry = parse_line(lines[i % len(lines)], day=DAY)
        entries.append(_retime(entry, DAY + timedelta(hours=9, seconds=2 * i)))
    return entries


def _retime(entry: TimelineEntry, ts: datetime) -> TimelineEntry:
    return type(entry)(ts, *[getattr(entry, f) for f in entry.__slots__[1:]])  # type: ignore[arg-type]


def test_feed_p99_under_5ms() -> None:
    entries = _meeting(3000)
    detector = NameTriggerDetector("Nick", ["Nicolas", "Nick Maglowsch"])
    for entry in entries[:100]:  # warm-up (regex compilation, caches)
        detector.feed(entry)
    timings = []
    for entry in entries[100:]:
        start = time.perf_counter()
        detector.feed(entry)
        timings.append(time.perf_counter() - start)
    timings.sort()
    p99 = timings[int(len(timings) * 0.99)]
    assert p99 < 0.005, f"p99={p99 * 1000:.2f} ms"
