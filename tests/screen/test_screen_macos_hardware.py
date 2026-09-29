"""Real-screen checks for MacScreenSource. Run by hand on a Mac: ``uv run pytest -m hardware``.

Needs the Screen Recording permission for the terminal running pytest and a Python
with tkinter (uv's managed Python has it). Durations can be shortened for a quick
smoke run with ``SOMBRA_HW_SECONDS=30``; the acceptance run uses the default 300 s.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from typing import Any

import pytest

from sombra.screen.macos import MacScreenSource, WindowSelector

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.macos,
    pytest.mark.skipif(sys.platform != "darwin", reason="macOS only"),
]

SECONDS = float(os.environ.get("SOMBRA_HW_SECONDS", "300"))
INTERVAL = 5.0
TARGET = "sombra-hw-target"
DISTRACTOR = "sombra-hw-distractor"

# A red target window with a green window on top of it, covering most of it.
_WINDOWS_SCRIPT = textwrap.dedent(
    f"""
    import tkinter as tk
    root = tk.Tk()
    root.title({TARGET!r})
    root.geometry("600x400+200+200")
    root.configure(bg="#ff0000")
    top = tk.Toplevel(root)
    top.title({DISTRACTOR!r})
    top.geometry("600x400+260+240")
    top.configure(bg="#00ff00")
    top.attributes("-topmost", True)
    root.mainloop()
    """
)


@pytest.fixture
def colored_windows() -> Iterator[None]:
    proc = subprocess.Popen([sys.executable, "-c", _WINDOWS_SCRIPT])  # noqa: S603
    time.sleep(2.0)  # let the window server list them
    try:
        yield
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _color_fractions(png: bytes, size: int = 64) -> tuple[float, float]:
    """Fraction of (red, green) pixels after scaling the PNG down to size x size RGBA."""
    import Quartz

    src = Quartz.CGImageSourceCreateWithData(png, None)
    image = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    buf = bytearray(size * size * 4)
    ctx = Quartz.CGBitmapContextCreate(
        buf,
        size,
        size,
        8,
        size * 4,
        Quartz.CGColorSpaceCreateDeviceRGB(),
        Quartz.kCGImageAlphaPremultipliedLast,
    )
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, size, size), image)
    red = green = 0
    for i in range(0, len(buf), 4):
        r, g, b = buf[i], buf[i + 1], buf[i + 2]
        red += r > 150 and g < 100 and b < 100
        green += g > 150 and r < 100 and b < 100
    n = size * size
    return red / n, green / n


def test_window_mode_captures_only_the_chosen_window(colored_windows: Any) -> None:
    src = MacScreenSource("window", selector=WindowSelector(title=TARGET))
    shots = 0
    deadline = time.monotonic() + SECONDS
    while time.monotonic() < deadline:
        shot = asyncio.run(src.grab())
        red, green = _color_fractions(shot.image)
        assert shot.window_title == TARGET
        assert red > 0.6, f"shot {shots}: only {red:.0%} red; did it capture the window?"
        assert green == 0, f"shot {shots}: {green:.1%} green leaked from the covering window"
        shots += 1
        time.sleep(INTERVAL)
    assert shots >= int(SECONDS // (INTERVAL + 1))


def test_grab_latency_and_loop_cpu() -> None:
    src = MacScreenSource("display")
    asyncio.run(src.grab())  # warm-up: framework load, permission check

    latencies = []
    for _ in range(20):
        t = time.perf_counter()
        asyncio.run(src.grab())
        latencies.append((time.perf_counter() - t) * 1000)
    p50 = statistics.median(latencies)

    wall0, cpu0 = time.monotonic(), time.process_time()
    deadline = wall0 + min(SECONDS, 120)
    while time.monotonic() < deadline:
        asyncio.run(src.grab())
        time.sleep(INTERVAL)
    cpu_pct = 100 * (time.process_time() - cpu0) / (time.monotonic() - wall0)

    sys.stderr.write(
        f"\ngrab p50={p50:.1f} ms max={max(latencies):.1f} ms; loop CPU {cpu_pct:.2f}%\n"
    )
    assert p50 < 100
    assert cpu_pct < 3
