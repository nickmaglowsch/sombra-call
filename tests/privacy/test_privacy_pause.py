import asyncio
import threading

import pytest

from sombra.privacy.pause import PauseController


def test_state_machine() -> None:
    c = PauseController()
    assert not c.is_paused
    assert c.pause() is True
    assert c.is_paused
    assert c.pause() is False  # already paused: no change
    assert c.resume() is True
    assert c.resume() is False
    assert c.toggle() is True
    assert c.is_paused
    assert c.toggle() is False
    assert not c.is_paused


def test_starts_paused_if_asked() -> None:
    assert PauseController(paused=True).is_paused


def test_subscribers_notified_only_on_change() -> None:
    c = PauseController()
    seen: list[bool] = []
    unsubscribe = c.subscribe(seen.append)
    c.pause()
    c.pause()
    c.toggle()
    c.resume()
    assert seen == [True, False]
    unsubscribe()
    unsubscribe()  # idempotent
    c.pause()
    assert seen == [True, False]


def test_failing_subscriber_does_not_block_others(caplog: pytest.LogCaptureFixture) -> None:
    c = PauseController()
    seen: list[bool] = []

    def boom(paused: bool) -> None:
        raise RuntimeError("broken")

    c.subscribe(boom)
    c.subscribe(seen.append)
    c.pause()
    assert seen == [True]
    assert "pause subscriber failed" in caplog.text


async def test_wait_resumed_returns_immediately_when_running() -> None:
    await asyncio.wait_for(PauseController().wait_resumed(), 1)


async def test_wait_resumed_blocks_until_resume() -> None:
    c = PauseController(paused=True)
    task = asyncio.create_task(c.wait_resumed())
    await asyncio.sleep(0.01)
    assert not task.done()
    c.pause()  # no change, still waiting
    await asyncio.sleep(0.01)
    assert not task.done()
    c.resume()
    await asyncio.wait_for(task, 1)


async def test_resume_from_another_thread_wakes_waiter() -> None:
    c = PauseController(paused=True)
    task = asyncio.create_task(c.wait_resumed())
    await asyncio.sleep(0.01)
    t = threading.Thread(target=c.resume)
    t.start()
    t.join()
    await asyncio.wait_for(task, 1)


async def test_cancelled_waiter_does_not_break_resume() -> None:
    c = PauseController(paused=True)
    task = asyncio.create_task(c.wait_resumed())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    c.resume()
    await asyncio.sleep(0.01)


def test_concurrent_toggles_are_consistent() -> None:
    c = PauseController()
    seen: list[bool] = []
    c.subscribe(seen.append)
    threads = [threading.Thread(target=c.toggle) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == 50  # every toggle is a real change
    assert c.is_paused is False  # even number of toggles
