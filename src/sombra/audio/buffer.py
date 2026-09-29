"""Bounded hand-off from capture threads to the asyncio consumer.

Capture callbacks run on audio threads and must never block. When the consumer
stalls, the oldest chunks are dropped (and counted) instead of growing memory or
stalling the device.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections import Counter, deque

from sombra.contracts import AudioChunk, Channel


class DropOldestQueue:
    """Thread-safe producer side, asyncio consumer side, fixed capacity."""

    def __init__(self, maxlen: int, loop: asyncio.AbstractEventLoop | None = None) -> None:
        if maxlen <= 0:
            raise ValueError("maxlen must be positive")
        self.maxlen = maxlen
        self._items: deque[AudioChunk] = deque()
        self._lock = threading.Lock()
        self._loop = loop
        self._ready = asyncio.Event()
        self._closed = False
        self.dropped: Counter[Channel] = Counter()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach the consumer's event loop (call from the consumer before capture starts)."""
        self._loop = loop

    @property
    def dropped_total(self) -> int:
        return sum(self.dropped.values())

    def __len__(self) -> int:
        return len(self._items)

    def put(self, chunk: AudioChunk) -> None:
        """Called from any thread. Never blocks beyond a short lock; drops oldest when full."""
        with self._lock:
            if self._closed:
                return
            if len(self._items) >= self.maxlen:
                old = self._items.popleft()
                self.dropped[old.channel] += 1
            self._items.append(chunk)
        self._wake()

    def close(self) -> None:
        """End the stream: the consumer drains what is left, then stops."""
        with self._lock:
            self._closed = True
        self._wake()

    def _wake(self) -> None:
        loop = self._loop
        if loop is None:
            self._ready.set()
            return
        # A closed loop means nobody is consuming any more.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._ready.set)

    async def get(self) -> AudioChunk | None:
        """Next chunk, or ``None`` once closed and drained."""
        while True:
            with self._lock:
                if self._items:
                    return self._items.popleft()
                if self._closed:
                    return None
                self._ready.clear()
            await self._ready.wait()
