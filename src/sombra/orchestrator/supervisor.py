"""Crash isolation for pipeline tasks.

Each pipeline runs as a child task under a supervisor. If the child raises, or is
cancelled by anyone but the supervisor, the error is logged and the child is
restarted after an exponential backoff; the other pipelines never notice. A child
that returns normally (its source ended) is not restarted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

log = logging.getLogger(__name__)

TaskFactory = Callable[[], Coroutine[Any, Any, None]]


class Supervised:
    """One named pipeline, restarted with backoff when it crashes."""

    def __init__(
        self,
        name: str,
        factory: TaskFactory,
        *,
        backoff_s: float,
        backoff_max_s: float,
    ) -> None:
        self.name = name
        self._factory = factory
        self._backoff_s = backoff_s
        self._backoff_max_s = backoff_max_s
        self.restarts = 0
        self.current: asyncio.Task[None] | None = None  # the running child, for diagnostics
        self._task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        self._task = asyncio.create_task(self._run(), name=f"supervise:{self.name}")
        return self._task

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    async def _run(self) -> None:
        delay = self._backoff_s
        while True:
            started = time.monotonic()
            child = asyncio.create_task(self._factory(), name=self.name)
            self.current = child
            try:
                await asyncio.wait({child})
            except asyncio.CancelledError:
                child.cancel()
                await asyncio.gather(child, return_exceptions=True)
                raise
            if child.cancelled():
                log.error("pipeline %s was cancelled; restarting", self.name)
            elif (exc := child.exception()) is not None:
                log.error("pipeline %s crashed; restarting", self.name, exc_info=exc)
            else:
                return
            if time.monotonic() - started > self._backoff_max_s:
                delay = self._backoff_s  # it ran fine for a while: start the backoff over
            self.restarts += 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._backoff_max_s)
