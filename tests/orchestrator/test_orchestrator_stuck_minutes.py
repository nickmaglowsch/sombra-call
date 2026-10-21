"""#83: a second Ctrl+C while the minutes wait on the model exits ``sombra start``.

The summary and minutes calls run on daemon threads (``wiring.in_daemon_thread``), so
neither the loop's executor shutdown nor the interpreter's exit waits for them, and
the append-only files are left intact.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
from stuck_minutes import SCRIPT

from sombra.orchestrator.live import ABORTED
from sombra.orchestrator.wiring import in_daemon_thread

TESTS = Path(__file__).resolve().parents[1]
SCRIPT_FILE = Path(__file__).with_name("stuck_minutes.py")
EXIT_WITHIN_S = 2.0


def _spawn(*args: str) -> tuple[subprocess.Popen[str], queue.Queue[str]]:
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(TESTS), str(TESTS / "orchestrator")]))
    proc = subprocess.Popen(  # noqa: S603  # our interpreter and our own test script
        [sys.executable, str(SCRIPT_FILE), *args],
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
        env=env,
    )
    lines: queue.Queue[str] = queue.Queue()

    def read() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            lines.put(line)

    threading.Thread(target=read, daemon=True).start()
    return proc, lines


def _wait_for(lines: queue.Queue[str], marker: str, seen: list[str], within: float = 30.0) -> None:
    deadline = time.monotonic() + within
    while marker not in "".join(seen):
        try:
            seen.append(lines.get(timeout=max(0.0, deadline - time.monotonic())))
        except queue.Empty:
            raise AssertionError(f"no {marker!r} on stderr; got {''.join(seen)!r}") from None


def _until(check: object, what: str, within: float = 30.0) -> None:
    deadline = time.monotonic() + within
    while not check():  # type: ignore[operator]
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def _meeting_file(root: Path, name: str) -> Path:
    (meeting,) = [p for p in root.iterdir() if p.is_dir()]
    return meeting / name


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["terminal", "window"])
def test_second_ctrl_c_during_the_minutes_exits_and_keeps_the_files(mode: str) -> None:
    with tempfile.TemporaryDirectory(prefix="sb", dir="/tmp") as tmp:
        root = Path(tmp)
        proc, lines = _spawn(mode, tmp)
        seen: list[str] = []
        try:
            _wait_for(lines, "CAPTURING", seen)
            transcript = _meeting_file(root, "transcript.md")
            _until(lambda: SCRIPT[0].text in transcript.read_text(encoding="utf-8"), "speech")
            proc.send_signal(signal.SIGINT)
            _wait_for(lines, "MINUTES", seen)  # the minutes call is now blocked for good
            before = transcript.read_bytes()
            assert proc.poll() is None
            t0 = time.monotonic()
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=30)
            elapsed = time.monotonic() - t0
        finally:
            proc.kill()
            proc.wait()
        _wait_for(lines, ABORTED.strip(), seen, within=5)
        assert elapsed < EXIT_WITHIN_S, f"took {elapsed:.2f} s"
        assert proc.returncode == -signal.SIGINT  # an uncaught KeyboardInterrupt
        after = transcript.read_bytes()
        assert after.startswith(before)  # append-only: nothing truncated
        assert SCRIPT[0].text.encode() in after
        assert after.endswith(b"\n")
        log = _meeting_file(root, "log.jsonl").read_bytes()
        assert not log or log.endswith(b"\n")
        for line in log.decode("utf-8").splitlines():
            json.loads(line)


# --- in_daemon_thread ------------------------------------------------------------------


async def test_in_daemon_thread_returns_the_result_from_another_daemon_thread() -> None:
    def call(a: int, *, b: int) -> tuple[int, bool, str]:
        thread = threading.current_thread()
        return a + b, thread.daemon, thread.name

    total, daemon, name = await in_daemon_thread(call, 1, b=2)
    assert (total, daemon) == (3, True)
    assert name == "sombra-call"
    assert name != threading.current_thread().name


async def test_in_daemon_thread_raises_the_call_s_error() -> None:
    def boom() -> None:
        raise ValueError("model down")

    with pytest.raises(ValueError, match="model down"):
        await in_daemon_thread(boom)


async def test_cancelling_the_await_abandons_a_stuck_call() -> None:
    release = threading.Event()
    started = threading.Event()

    def stuck() -> str:
        started.set()
        release.wait()
        return "late"

    task = asyncio.create_task(in_daemon_thread(stuck))
    await asyncio.to_thread(started.wait)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()  # its late result lands on a cancelled future and is dropped
    await asyncio.sleep(0.05)


def test_a_call_that_outlives_its_loop_finishes_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    errors: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, "excepthook", errors.append)

    async def abandon() -> None:
        task = asyncio.create_task(in_daemon_thread(release.wait))
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(abandon())  # closes the loop while the call still runs
    (thread,) = [t for t in threading.enumerate() if t.name == "sombra-wait"]
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []  # call_soon_threadsafe on the closed loop did not escape
