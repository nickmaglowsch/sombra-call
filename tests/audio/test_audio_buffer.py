import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from sombra.audio.buffer import DropOldestQueue
from sombra.contracts import AudioChunk, Channel

T0 = datetime(2026, 9, 29, 14, 30, tzinfo=UTC)


def _chunk(i: int, ch: Channel = Channel.ME) -> AudioChunk:
    return AudioChunk(channel=ch, start=T0 + timedelta(milliseconds=50 * i), pcm_f32le=b"")


def test_maxlen_must_be_positive() -> None:
    with pytest.raises(ValueError):
        DropOldestQueue(0)


async def test_stalled_consumer_drops_oldest_and_counts() -> None:
    q = DropOldestQueue(3, asyncio.get_running_loop())
    for i in range(5):
        q.put(_chunk(i, Channel.ME if i % 2 == 0 else Channel.OTHERS))
    assert len(q) == 3
    assert q.dropped == {Channel.ME: 1, Channel.OTHERS: 1}
    assert q.dropped_total == 2
    q.close()
    got = []
    while (c := await q.get()) is not None:
        got.append(c.start)
    assert got == [T0 + timedelta(milliseconds=50 * i) for i in (2, 3, 4)]


async def test_producer_thread_never_blocks_on_stalled_consumer() -> None:
    q = DropOldestQueue(10, asyncio.get_running_loop())

    def produce() -> None:
        for i in range(10_000):
            q.put(_chunk(i))

    t = threading.Thread(target=produce)
    started = time.perf_counter()
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()
    assert time.perf_counter() - started < 5
    assert q.dropped_total == 9_990
    assert (await q.get()) == _chunk(9_990)


async def test_consumer_wakes_on_put_from_other_thread() -> None:
    q = DropOldestQueue(10)
    q.bind(asyncio.get_running_loop())
    getter = asyncio.create_task(q.get())
    await asyncio.sleep(0.01)
    threading.Thread(target=q.put, args=(_chunk(1),)).start()
    assert await asyncio.wait_for(getter, 2) == _chunk(1)


async def test_close_ends_stream_and_ignores_later_puts() -> None:
    q = DropOldestQueue(2)
    getter = asyncio.create_task(q.get())
    await asyncio.sleep(0)
    q.close()
    assert await asyncio.wait_for(getter, 2) is None
    q.put(_chunk(1))
    assert len(q) == 0


def test_put_after_loop_closed_does_not_raise() -> None:
    loop = asyncio.new_event_loop()
    q = DropOldestQueue(2, loop)
    loop.close()
    q.put(_chunk(1))
    assert len(q) == 1
