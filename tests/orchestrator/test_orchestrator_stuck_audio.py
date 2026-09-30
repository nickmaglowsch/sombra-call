"""#75: a live session whose audio stop hangs in Core Audio still ends.

It writes its minutes after ``MacAudioSource``'s close timeout, and a second Ctrl+C
during the stuck shutdown exits ``sombra start``.
"""

from __future__ import annotations

import asyncio
import os
import queue
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from orchestrator_harness import until
from stuck_audio import MINUTES, SCRIPT, StuckPortAudio, live_plan, stuck_source

from fakes import FakeScreenSource, FakeTranscriber
from sombra.orchestrator.live import StopSignal, join_interruptibly, run_live
from sombra.summary import read_minutes_section

TESTS = Path(__file__).resolve().parents[1]
CLOSE_TIMEOUT_S = 0.3


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    """Short dir for Unix sockets (macOS caps AF_UNIX paths at 104 bytes)."""
    with tempfile.TemporaryDirectory(prefix="sb", dir="/tmp") as d:
        yield Path(d)


async def test_minutes_are_written_when_the_audio_stop_hangs(
    tmp_path: Path, socket_dir: Path
) -> None:
    plan = live_plan(tmp_path, summary=True)
    sd = StuckPortAudio()
    stop = StopSignal()
    transcript = plan.meeting_dir / "transcript.md"
    stopped_at: list[float] = []

    async def speak_then_stop() -> None:
        await until(lambda: len(sd.streams) == 2, what="both streams open")
        await asyncio.to_thread(sd.speak, 0.5)
        await until(lambda: SCRIPT[0].text in transcript.read_text(encoding="utf-8"))
        stopped_at.append(time.monotonic())
        stop.request()

    driver = asyncio.create_task(speak_then_stop())
    try:
        await asyncio.wait_for(
            run_live(
                plan,
                stop,
                sources=lambda p: (stuck_source(sd, CLOSE_TIMEOUT_S), FakeScreenSource()),
                transcriber=FakeTranscriber(SCRIPT),
                control_socket=socket_dir / "control.sock",
            ),
            timeout=15,
        )
        await driver
        shutdown_s = time.monotonic() - stopped_at[0]
    finally:
        sd.release()
    assert sd.stuck.is_set()  # the stop really hung
    assert shutdown_s < CLOSE_TIMEOUT_S + 2.0
    minutes = read_minutes_section(plan.meeting_dir / "summary.md") or ""
    assert MINUTES["resumo"] in minutes


SOMBRA_START = """
import sys
from pathlib import Path

from fakes import FakeScreenSource, FakeTranscriber
from orchestrator_harness import until
from stuck_audio import StuckPortAudio, live_plan, stuck_source

from sombra.orchestrator.live import run_in_terminal

root = Path(sys.argv[1])
sd = StuckPortAudio(announce=True)


def sources(plan):
    sys.stderr.write("CAPTURING\\n")
    sys.stderr.flush()
    return stuck_source(sd, close_timeout_s=600), FakeScreenSource()


run_in_terminal(
    live_plan(root),
    sources=sources,
    transcriber=FakeTranscriber([]),
    control_socket=root / "c.sock",
)
"""


def _spawn(code: str, *args: str) -> tuple[subprocess.Popen[str], queue.Queue[str]]:
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(TESTS), str(TESTS / "orchestrator")]))
    proc = subprocess.Popen(  # noqa: S603  # our interpreter and our own test script
        [sys.executable, "-c", textwrap.dedent(code), *args],
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


def _wait_for(lines: queue.Queue[str], marker: str, within: float = 20.0) -> None:
    seen: list[str] = []
    deadline = time.monotonic() + within
    while marker not in "".join(seen):
        try:
            seen.append(lines.get(timeout=max(0.0, deadline - time.monotonic())))
        except queue.Empty:
            raise AssertionError(f"no {marker!r} on stderr; got {''.join(seen)!r}") from None


@pytest.mark.slow
def test_second_ctrl_c_exits_sombra_start_while_audio_is_stuck() -> None:
    with tempfile.TemporaryDirectory(prefix="sb", dir="/tmp") as root:
        proc, lines = _spawn(SOMBRA_START, root)
        try:
            _wait_for(lines, "CAPTURING")
            proc.send_signal(signal.SIGINT)
            _wait_for(lines, "STUCK")  # the audio teardown is hung in "Core Audio"
            time.sleep(0.2)
            assert proc.poll() is None  # the close timeout (600 s) has not run out
            t0 = time.monotonic()
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=10)
            assert time.monotonic() - t0 < 5
            assert proc.returncode != 0  # aborted by the KeyboardInterrupt
        finally:
            proc.kill()
            proc.wait()


JOIN = """
import sys
import threading

from sombra.orchestrator.live import join_interruptibly

thread = threading.Thread(target=threading.Event().wait, daemon=True)
thread.start()
sys.stderr.write("JOINING\\n")
sys.stderr.flush()
try:
    join_interruptibly(thread)
except KeyboardInterrupt:
    sys.exit(3)
"""


@pytest.mark.slow
def test_ctrl_c_interrupts_the_join_on_the_session_thread() -> None:
    """``run_with_window``'s main thread waits for the session thread like this."""
    proc, lines = _spawn(JOIN)
    try:
        _wait_for(lines, "JOINING")
        time.sleep(0.1)
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=5) == 3
    finally:
        proc.kill()
        proc.wait()


def test_join_interruptibly_returns_when_the_thread_ends() -> None:
    done = threading.Event()
    thread = threading.Thread(target=done.wait, daemon=True)
    thread.start()
    threading.Timer(0.05, done.set).start()
    join_interruptibly(thread, poll_s=0.01)
    assert not thread.is_alive()
