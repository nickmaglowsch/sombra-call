import asyncio
import os
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sombra.contracts import (
    ActionKind,
    ActionLogged,
    Channel,
    FrameMarker,
    FrameRecord,
    SpeechLine,
    TimelineStore,
    log_event_type,
    parse_line,
)
from sombra.store import MeetingStore, create_meeting

TZ = timezone(timedelta(hours=-3))
START = datetime(2026, 9, 29, 14, 30, 0, tzinfo=TZ)


@pytest.fixture
def meeting(tmp_path: Path) -> Path:
    return create_meeting(tmp_path, "Daily time X", started_at=START)


@pytest.fixture
def store(meeting: Path) -> Iterator[MeetingStore]:
    s = MeetingStore(meeting)
    yield s
    s.close()


def _speech(i: int, text: str = "acho que dá pra fechar na sexta") -> SpeechLine:
    ch = Channel.ME if i % 2 else Channel.OTHERS
    return SpeechLine(ts=START + timedelta(seconds=i), channel=ch, text=f"{text} #{i}")


def _frame(n: int, ts: datetime, title: str | None = "Zoom - Roadmap Q4") -> FrameRecord:
    return FrameRecord(
        id=f"f{n:04d}",
        ts=ts,
        path=f"frames/f{n:04d}.jpg",
        width=1280,
        height=720,
        app="zoom.us",
        window_title=title,
        diff_score=12.0,
        phash="deadbeefdeadbeef",
    )


def test_implements_timeline_store_port(store: MeetingStore, meeting: Path) -> None:
    port: TimelineStore = store
    assert port.meeting_dir == meeting


def test_append_entry_writes_one_line(store: MeetingStore, meeting: Path) -> None:
    store.append_entry(_speech(1))
    text = (meeting / "transcript.md").read_text(encoding="utf-8")
    assert text == "[14:30:01] EU: acho que dá pra fechar na sexta #1\n"


def test_concurrent_appends_from_threads_and_tasks(store: MeetingStore, meeting: Path) -> None:
    """4 threads + 2 asyncio tasks, 1,000 appends -> 1,000 intact, parseable lines."""
    per_writer = 1000 // 6
    counts = [per_writer] * 6
    counts[0] += 1000 - sum(counts)
    barrier = threading.Barrier(4)

    def thread_writer(w: int) -> None:
        barrier.wait()
        for i in range(counts[w]):
            store.append_entry(_speech(i, f"thread {w} " + "blá " * 50))

    async def task_writer(w: int) -> None:
        for i in range(counts[w]):
            store.append_entry(_speech(i, f"task {w} olá"))
            if i % 10 == 0:
                await asyncio.sleep(0)

    async def run_tasks() -> None:
        await asyncio.gather(task_writer(4), task_writer(5))

    threads = [threading.Thread(target=thread_writer, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    asyncio.run(run_tasks())
    for t in threads:
        t.join()

    raw = (meeting / "transcript.md").read_text(encoding="utf-8")
    lines = raw.splitlines()
    assert raw.endswith("\n")
    assert len(lines) == 1000
    entries = [parse_line(line, day=START) for line in lines]
    assert all(isinstance(e, SpeechLine) for e in entries)
    for w in range(6):
        mine = [e for e in entries if isinstance(e, SpeechLine) and f" {w} " in e.text]
        assert len(mine) == counts[w]
    assert len(store.entries_since(START)) == 1000


def test_store_never_rewrites(store: MeetingStore, meeting: Path) -> None:
    """Size is monotonic and the first N bytes never change as writes continue."""
    files = [meeting / "transcript.md", meeting / "frames/index.jsonl", meeting / "log.jsonl"]
    snapshots = {f: f.read_bytes() for f in files}
    for i in range(50):
        store.append_entry(_speech(i))
        if i % 5 == 0:
            store.append_frame(_frame(i, START + timedelta(seconds=i)))
        store.log(ActionLogged(suggestion_id=f"s{i}", ts=START, kind=ActionKind.APPROVE))
        for f in files:
            now = f.read_bytes()
            before = snapshots[f]
            assert len(now) >= len(before)
            assert now[: len(before)] == before
            snapshots[f] = now
    assert all(snapshots[f] for f in files)


def test_append_frame_writes_index_and_marker_with_same_id_and_ts(
    store: MeetingStore, meeting: Path
) -> None:
    ts = START + timedelta(minutes=2, seconds=10)
    store.append_frame(_frame(123, ts))

    index = (meeting / "frames/index.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(index) == 1
    assert '"id":"f0123"' in index[0]
    assert f'"ts":"{ts.isoformat()}"' in index[0]

    lines = (meeting / "transcript.md").read_text(encoding="utf-8").splitlines()
    assert lines == ['[14:32:10] TELA f0123 "Zoom - Roadmap Q4"']
    marker = parse_line(lines[0], day=START)
    assert marker == FrameMarker(ts=ts, frame_id="f0123", window_title="Zoom - Roadmap Q4")


def test_frame_marker_falls_back_to_app_then_empty(store: MeetingStore, meeting: Path) -> None:
    store.append_frame(_frame(1, START, title=None))
    store.append_frame(replace(_frame(2, START, title=None), app=None))
    lines = (meeting / "transcript.md").read_text(encoding="utf-8").splitlines()
    assert lines == ['[14:30:00] TELA f0001 "zoom.us"', '[14:30:00] TELA f0002 ""']


def test_log_writes_json_line(store: MeetingStore, meeting: Path) -> None:
    store.log(ActionLogged(suggestion_id="s1", ts=START, kind=ActionKind.EDIT, final_text="ok"))
    lines = (meeting / "log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert log_event_type(lines[0]) == "action"


def test_entries_since_filters_and_skips_header(tmp_path: Path) -> None:
    meeting = create_meeting(tmp_path, "x", started_at=START, header="# Daily\n\nparticipantes: A")
    with MeetingStore(meeting) as store:
        for i in range(5):
            store.append_entry(_speech(i * 10))
        got = store.entries_since(START + timedelta(seconds=20))
    assert [e.ts for e in got] == [START + timedelta(seconds=s) for s in (20, 30, 40)]


def test_entries_since_accepts_naive_since(store: MeetingStore) -> None:
    store.append_entry(_speech(5))
    assert len(store.entries_since(datetime(2026, 9, 29, 14, 30, 0))) == 1
    assert store.entries_since(datetime(2026, 9, 29, 14, 31, 0)) == []


def test_entries_since_with_naive_meeting_and_aware_since(meeting: Path) -> None:
    naive_start = datetime(2026, 9, 29, 14, 30, 0)
    with MeetingStore(meeting, started_at=naive_start) as store:
        store.append_entry(SpeechLine(ts=naive_start, channel=Channel.ME, text="oi"))
        assert store.entries_since(naive_start.astimezone() - timedelta(hours=1)) != []


def test_entries_since_handles_midnight_rollover(tmp_path: Path) -> None:
    late = datetime(2026, 9, 29, 23, 59, 50, tzinfo=TZ)
    meeting = create_meeting(tmp_path, "late", started_at=late)
    with MeetingStore(meeting) as store:
        store.append_entry(SpeechLine(ts=late, channel=Channel.ME, text="boa noite"))
        store.append_entry(
            SpeechLine(ts=late + timedelta(seconds=20), channel=Channel.OTHERS, text="já é amanhã")
        )
        got = store.entries_since(late + timedelta(seconds=1))
    assert [e.ts for e in got] == [datetime(2026, 9, 30, 0, 0, 10, tzinfo=TZ)]


def test_truncated_last_line_is_readable(meeting: Path) -> None:
    """Simulated crash: the process died mid-write, leaving no final newline."""
    transcript = meeting / "transcript.md"
    full = _speech(1).to_line() + "\n"
    partial = _speech(2, "Nick, o que você acha").to_line().encode("utf-8")
    # cut inside a multi-byte character to make it as nasty as possible
    cut = partial.index("ê".encode()) + 1
    transcript.write_bytes(full.encode("utf-8") + partial[:cut])

    with MeetingStore(meeting) as store:
        got = store.entries_since(START)
        assert len(got) == 2
        assert isinstance(got[1], SpeechLine)
        assert got[1].text.startswith("Nick, o que voc")

        # reopening terminated the torn line, so the next append is its own line
        store.append_entry(_speech(3))
        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
        assert lines[-1] == _speech(3).to_line()
        assert len(store.entries_since(START)) == 3


def test_unparseable_torn_line_is_skipped(meeting: Path) -> None:
    (meeting / "transcript.md").write_bytes(_speech(1).to_line().encode() + b"\n[14:3")
    with MeetingStore(meeting) as store:
        assert len(store.entries_since(START)) == 1


def test_background_fsync(meeting: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced = threading.Event()
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        real_fsync(fd)
        synced.set()

    monkeypatch.setattr(os, "fsync", spy)
    with MeetingStore(meeting, fsync_interval=0.01) as store:
        store.append_entry(_speech(1))
        assert synced.wait(timeout=5)


def test_close_is_idempotent_and_blocks_writes(meeting: Path) -> None:
    store = MeetingStore(meeting)
    store.close()
    store.close()
    store.sync()  # no-op once closed
    with pytest.raises(RuntimeError, match="closed"):
        store.append_entry(_speech(1))


def test_rejects_bad_fsync_interval(meeting: Path) -> None:
    with pytest.raises(ValueError, match="fsync_interval"):
        MeetingStore(meeting, fsync_interval=0)


def test_aware_times_are_written_in_meeting_timezone(store: MeetingStore, meeting: Path) -> None:
    utc_ts = datetime(2026, 9, 29, 17, 31, 0, tzinfo=UTC)  # 14:31 at UTC-3
    store.append_entry(SpeechLine(ts=utc_ts, channel=Channel.ME, text="oi"))
    store.append_frame(_frame(1, utc_ts))
    lines = (meeting / "transcript.md").read_text(encoding="utf-8").splitlines()
    assert [line[:10] for line in lines] == ["[14:31:00]", "[14:31:00]"]
    assert [e.ts for e in store.entries_since(START)] == [utc_ts, utc_ts]


def test_naive_meeting_writes_aware_times_as_local(meeting: Path) -> None:
    naive_start = datetime(2026, 9, 29, 14, 30, 0)
    aware = naive_start.astimezone()
    with MeetingStore(meeting, started_at=naive_start) as store:
        store.append_entry(SpeechLine(ts=aware, channel=Channel.ME, text="oi"))
        assert [e.ts for e in store.entries_since(naive_start)] == [naive_start]


def test_entries_since_first_line_after_midnight(tmp_path: Path) -> None:
    """Regression: a meeting starting 23:58 whose first line is 00:01 the next day."""
    start = datetime(2026, 9, 29, 23, 58, tzinfo=TZ)
    meeting = create_meeting(tmp_path, "late", started_at=start)
    first = datetime(2026, 9, 30, 0, 1, tzinfo=TZ)
    with MeetingStore(meeting) as store:
        store.append_entry(SpeechLine(ts=first, channel=Channel.ME, text="oi"))
        store.append_entry(
            SpeechLine(ts=first + timedelta(minutes=1), channel=Channel.OTHERS, text="tudo bem?")
        )
        got = store.entries_since(datetime(2026, 9, 30, 0, 0, tzinfo=TZ))
    assert [e.ts for e in got] == [first, first + timedelta(minutes=1)]


class _ShortWriter:
    """Wraps a raw file and writes at most 3 bytes per call, like a short write."""

    def __init__(self, f: object) -> None:
        self._f = f
        self.name = "short"

    def write(self, data: memoryview) -> int:
        return self._f.write(data[:3])  # type: ignore[attr-defined,no-any-return]

    def fileno(self) -> int:
        return self._f.fileno()  # type: ignore[attr-defined,no-any-return]

    def close(self) -> None:
        self._f.close()  # type: ignore[attr-defined]


def test_short_writes_are_completed(store: MeetingStore, meeting: Path) -> None:
    store._transcript = _ShortWriter(store._transcript)  # type: ignore[assignment]
    store.append_entry(_speech(1))
    text = (meeting / "transcript.md").read_text(encoding="utf-8")
    assert text == _speech(1).to_line() + "\n"


def test_zero_byte_write_raises(store: MeetingStore) -> None:
    class _Stuck:
        name = "stuck"

        def write(self, data: memoryview) -> int:
            return 0

    real = store._log
    store._log = _Stuck()  # type: ignore[assignment]
    try:
        with pytest.raises(OSError, match="could not write"):
            store.log(ActionLogged(suggestion_id="s", ts=START, kind=ActionKind.APPROVE))
    finally:
        store._log = real


def test_fsync_failure_is_retried_and_thread_survives(
    meeting: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    recovered = threading.Event()
    real_fsync = os.fsync

    def flaky(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(5, "EIO")
        real_fsync(fd)
        recovered.set()

    monkeypatch.setattr(os, "fsync", flaky)
    with MeetingStore(meeting, fsync_interval=0.01) as store:
        store.append_entry(_speech(1))
        assert recovered.wait(timeout=5)  # same file retried after the failure
        assert store._syncer.is_alive()


def test_explicit_sync_raises_and_keeps_file_dirty(
    store: MeetingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store._stop.set()
    store._syncer.join()
    store.append_entry(_speech(1))

    def boom(fd: int) -> None:
        raise OSError(5, "EIO")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        store.sync()
    assert store._transcript in store._dirty
