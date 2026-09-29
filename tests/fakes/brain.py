"""``Brain`` fake with configurable latency, failures and hangs."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from sombra.contracts import BrainRequest, BrainResponse, Usage


class FakeBrain:
    """Answers ``"resposta <n>"``; ``frames_sent`` are the frame ids of the paths it got.

    ``fail_on`` / ``hang_on`` hold 0-based call numbers that raise ``RuntimeError``
    or never return (to exercise the orchestrator's timeout).
    """

    def __init__(
        self,
        *,
        latency_s: float = 0.0,
        fail_on: set[int] | None = None,
        hang_on: set[int] | None = None,
        fail_start: bool = False,
    ) -> None:
        self.latency_s = latency_s
        self.fail_on = set(fail_on or ())
        self.hang_on = set(hang_on or ())
        self.fail_start = fail_start
        self.meeting_dir: Path | None = None
        self.requests: list[BrainRequest] = []
        self.called_at: list[float] = []  # time.perf_counter() at each answer() call
        self.in_flight = 0
        self.max_in_flight = 0
        self.started = False
        self.closed = False

    async def start(self, meeting_dir: Path) -> None:
        if self.fail_start:
            raise RuntimeError("fake agent failed to start")
        self.meeting_dir = meeting_dir
        self.started = True

    async def answer(self, request: BrainRequest) -> BrainResponse:
        self.called_at.append(time.perf_counter())
        n = len(self.requests)
        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if n in self.hang_on:
                await asyncio.Event().wait()
            await asyncio.sleep(self.latency_s)
            if n in self.fail_on:
                raise RuntimeError(f"fake brain call {n} failed")
        finally:
            self.in_flight -= 1
        return BrainResponse(
            text=f"resposta {n + 1}",
            frames_sent=[p.stem for p in request.frame_paths],
            backend="fake",
            model="fake-model",
            usage=Usage(input_tokens=100, output_tokens=20),
        )

    async def close(self) -> None:
        self.closed = True
