"""Pause state for capture and STT, shared by the hotkey, the control socket and the orchestrator.

``PauseController`` is thread-safe (the macOS hotkey fires on the AppKit thread) and
async-friendly: subscribers are called synchronously on every state change, and
``wait_resumed()`` lets an asyncio task block until capture may continue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

Subscriber = Callable[[bool], None]  # receives the new is_paused value


class PauseController:
    def __init__(self, paused: bool = False) -> None:
        self._lock = threading.Lock()
        # Held across set + notify so subscribers see changes in the order they happened.
        # Re-entrant so a subscriber may itself call pause()/resume().
        self._notify_lock = threading.RLock()
        self._paused = paused
        self._subscribers: list[Subscriber] = []
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def pause(self) -> bool:
        """Pause capture. Returns True if the state changed."""
        return self._set(True)[0]

    def resume(self) -> bool:
        """Resume capture. Returns True if the state changed."""
        return self._set(False)[0]

    def toggle(self) -> bool:
        """Flip the state atomically. Returns the new ``is_paused``."""
        return self._set(None)[1]

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Call ``callback(is_paused)`` on every change. Returns an unsubscribe function."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    async def wait_resumed(self) -> None:
        """Return immediately if running, else wait until ``resume()`` is called."""
        loop = asyncio.get_running_loop()
        with self._lock:
            if not self._paused:
                return
            fut: asyncio.Future[None] = loop.create_future()
            waiter = (loop, fut)
            self._waiters.append(waiter)
        try:
            await fut
        finally:
            with self._lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)

    def _set(self, target: bool | None) -> tuple[bool, bool]:
        """Set the state (``None`` flips it) under one lock, then notify outside it.

        Returns ``(changed, is_paused)``.
        """
        with self._notify_lock:
            with self._lock:
                paused = (not self._paused) if target is None else target
                if self._paused == paused:
                    return False, paused
                self._paused = paused
                subscribers = list(self._subscribers)
                waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []
                if not paused:
                    waiters, self._waiters = self._waiters, []
            for loop, fut in waiters:
                # A waiter's loop may have closed since; that must not break resume().
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(_resolve, fut)
            for callback in subscribers:
                try:
                    callback(paused)
                except Exception:
                    # One broken subscriber must not keep the others (e.g. capture) running.
                    log.exception("pause subscriber failed")
            return True, paused


def _resolve(fut: asyncio.Future[None]) -> None:
    if not fut.done():
        fut.set_result(None)
