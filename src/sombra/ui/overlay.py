"""``OverlayUI``: the ``ApprovalUI`` implementation behind the approval window (O1, O2)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable
from pathlib import Path

from sombra.contracts import ActionKind, ApprovalUI, Suggestion, TriggerEvent, UserAction
from sombra.ui.server import OverlayServer
from sombra.ui.state import OverlayState, ProtocolError
from sombra.ui.system import Clipboard, Notifier, system_clipboard, system_notify

log = logging.getLogger(__name__)

SEARCHING_TITLE = "Sombra: buscando contexto…"
FAILURE_TITLE = "Sombra: responda manualmente"


class OverlayUI(ApprovalUI):
    """Serves the overlay page on loopback and turns clicks/shortcuts into ``UserAction``s.

    Usage::

        ui = OverlayUI(frames_dir=meeting_dir / "frames")
        await ui.start()
        open_window(ui.url)          # sombra.ui.window, or any browser
        await ui.show(suggestion)
        async for action in ui.actions(): ...
        await ui.close()

    ``copy_on_approve`` (text mode) puts the approved or edited text on the clipboard.
    ``notifier`` / ``clipboard`` default to the OS tools; pass fakes in tests.
    """

    def __init__(
        self,
        *,
        frames_dir: Path | None = None,
        copy_on_approve: bool = True,
        notifier: Notifier = system_notify,
        clipboard: Clipboard = system_clipboard,
        token: str | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._state = OverlayState()
        self._copy_on_approve = copy_on_approve
        self._notifier = notifier
        self._clipboard = clipboard
        self._actions: asyncio.Queue[UserAction | None] = asyncio.Queue()
        self._background: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._server = OverlayServer(
            snapshot=self._state.snapshot,
            on_message=self._on_message,
            frames_dir=frames_dir,
            token=token,
            host=host,
            port=port,
        )
        self.last_show_ms: float | None = None  # show() → pushed to every client

    # --- lifecycle -----------------------------------------------------------------

    @property
    def url(self) -> str:
        return self._server.url

    @property
    def token(self) -> str:
        return self._server.token

    @property
    def server(self) -> OverlayServer:
        return self._server

    @property
    def state(self) -> OverlayState:
        return self._state

    async def start(self) -> None:
        await self._server.start()

    async def close(self) -> None:
        await self._server.close()
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        self._actions.put_nowait(None)

    async def __aenter__(self) -> OverlayUI:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # --- ApprovalUI --------------------------------------------------------------------

    async def notify_trigger(self, trigger: TriggerEvent) -> None:
        async with self._lock:
            self._state.trigger(trigger.id, trigger.question)
            await self._push()
        self._spawn(self._notifier(SEARCHING_TITLE, trigger.question))

    async def show(self, suggestion: Suggestion) -> None:
        start = time.perf_counter()
        async with self._lock:
            self._state.suggestion(suggestion)
            await self._push()
        self.last_show_ms = (time.perf_counter() - start) * 1000

    async def notify_failure(self, trigger: TriggerEvent, reason: str) -> None:
        async with self._lock:
            self._state.failure(trigger.id, trigger.question, reason)
            await self._push()
        self._spawn(self._notifier(FAILURE_TITLE, trigger.question))

    async def actions(self) -> AsyncIterator[UserAction]:
        """Yields each decision once, in the order the user made them, until ``close()``."""
        while (action := await self._actions.get()) is not None:
            yield action

    # --- internals -------------------------------------------------------------------

    async def _push(self) -> None:
        await self._server.broadcast(self._state.snapshot())

    async def _on_message(self, raw: str) -> str | None:
        async with self._lock:
            try:
                action = self._state.handle(raw)
            except ProtocolError as e:
                return str(e)
            await self._push()
        if action is not None:
            self._actions.put_nowait(action)
            if self._copy_on_approve and action.text and action.kind in _COPY_KINDS:
                self._spawn(self._clipboard(action.text))
        return None

    def _spawn(self, aw: Awaitable[None]) -> None:
        """Run a notification/clipboard call without holding up the overlay."""
        task = asyncio.create_task(_quietly(aw))
        self._background.add(task)
        task.add_done_callback(self._background.discard)


_COPY_KINDS = frozenset({ActionKind.APPROVE, ActionKind.EDIT})


async def _quietly(aw: Awaitable[None]) -> None:
    try:
        await aw
    except Exception:  # a desktop side effect must never break the overlay
        log.exception("overlay side effect failed")
