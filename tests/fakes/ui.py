"""``ApprovalUI`` fake: records what it was shown and replays scripted user actions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

from sombra.contracts import ActionKind, Suggestion, TriggerEvent, UserAction

ScriptedAction = tuple[ActionKind, str | None]


class FakeUI:
    """Each ``show()`` pops the next scripted ``(kind, text)`` and emits it as a UserAction.

    With the script empty the suggestion just sits in the overlay (no action).
    """

    def __init__(self, actions: Iterable[ScriptedAction] = ()) -> None:
        self.script = list(actions)
        self.notified: list[TriggerEvent] = []
        self.shown: list[Suggestion] = []
        self.failures: list[tuple[TriggerEvent, str]] = []
        self._queue: asyncio.Queue[UserAction] = asyncio.Queue()

    async def notify_trigger(self, trigger: TriggerEvent) -> None:
        self.notified.append(trigger)

    async def show(self, suggestion: Suggestion) -> None:
        self.shown.append(suggestion)
        if self.script:
            kind, text = self.script.pop(0)
            self.emit(UserAction(suggestion_id=suggestion.id, kind=kind, text=text))

    async def notify_failure(self, trigger: TriggerEvent, reason: str) -> None:
        self.failures.append((trigger, reason))

    def emit(self, action: UserAction) -> None:
        """Simulate the user clicking in the overlay."""
        self._queue.put_nowait(action)

    async def actions(self) -> AsyncIterator[UserAction]:
        while True:
            yield await self._queue.get()
