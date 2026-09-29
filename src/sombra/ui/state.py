"""Pure overlay state: the card queue and the client-message → ``UserAction`` mapping.

No I/O here, so the whole protocol is unit-testable without a server or a window.

Cards, newest on top:

- ``searching``: a trigger fired and the brain is working ("buscando contexto…", O2).
- ``suggestion``: an answer waiting for approve / edit / discard / "não era comigo".
- ``failure``: the agent failed; the excerpt stays up so the user can answer manually.

A suggestion or failure replaces the ``searching`` card of the same trigger.

Client → server messages (JSON)::

    {"type": "action", "suggestion_id": "s1", "kind": "approve"}
    {"type": "action", "suggestion_id": "s1", "kind": "edit", "text": "nova resposta"}
    {"type": "action", "suggestion_id": "s1", "kind": "discard"}
    {"type": "action", "suggestion_id": "s1", "kind": "not_for_me"}
    {"type": "dismiss", "card_id": "t:tr1"}          # searching / failure cards only

Server → client: ``{"type": "state", "cards": [...]}`` after every change and on
connect, ``{"type": "error", "message": "..."}`` for a rejected message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sombra.contracts import ActionKind, Suggestion, UserAction

MAX_MESSAGE_BYTES = 64 * 1024


class ProtocolError(ValueError):
    """A client message that is malformed or refers to a card that is not pending."""


class CardKind(StrEnum):
    SEARCHING = "searching"
    SUGGESTION = "suggestion"
    FAILURE = "failure"


@dataclass(slots=True)
class Card:
    id: str  # "s:<suggestion id>" or "t:<trigger id>"
    kind: CardKind
    trigger_id: str
    excerpt: str
    seq: int
    suggestion_id: str | None = None
    text: str = ""
    frames: tuple[str, ...] = ()
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "trigger_id": self.trigger_id,
            "suggestion_id": self.suggestion_id,
            "excerpt": self.excerpt,
            "text": self.text,
            "frames": list(self.frames),
            "reason": self.reason,
        }


def _trigger_card_id(trigger_id: str) -> str:
    return f"t:{trigger_id}"


def _suggestion_card_id(suggestion_id: str) -> str:
    return f"s:{suggestion_id}"


@dataclass(slots=True)
class OverlayState:
    _cards: dict[str, Card] = field(default_factory=dict)
    _seq: int = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # --- server-side events --------------------------------------------------------

    def trigger(self, trigger_id: str, excerpt: str) -> Card:
        card = Card(
            id=_trigger_card_id(trigger_id),
            kind=CardKind.SEARCHING,
            trigger_id=trigger_id,
            excerpt=excerpt,
            seq=self._next_seq(),
        )
        self._cards[card.id] = card
        return card

    def suggestion(self, s: Suggestion) -> Card:
        self._cards.pop(_trigger_card_id(s.trigger_id), None)
        card = Card(
            id=_suggestion_card_id(s.id),
            kind=CardKind.SUGGESTION,
            trigger_id=s.trigger_id,
            suggestion_id=s.id,
            excerpt=s.excerpt,
            text=s.text,
            frames=tuple(s.frames_sent),
            seq=self._next_seq(),
        )
        self._cards[card.id] = card
        return card

    def failure(self, trigger_id: str, excerpt: str, reason: str) -> Card:
        card = Card(
            id=_trigger_card_id(trigger_id),
            kind=CardKind.FAILURE,
            trigger_id=trigger_id,
            excerpt=excerpt,
            reason=reason,
            seq=self._next_seq(),
        )
        self._cards[card.id] = card
        return card

    # --- queries ---------------------------------------------------------------------

    def cards(self) -> list[Card]:
        """Cards, newest first."""
        return sorted(self._cards.values(), key=lambda c: c.seq, reverse=True)

    def snapshot(self) -> dict[str, Any]:
        return {"type": "state", "cards": [c.to_json() for c in self.cards()]}

    # --- client messages -------------------------------------------------------------

    def handle(self, raw: str) -> UserAction | None:
        """Apply one client message. Returns the resulting action, or None for a dismiss.

        Raises ``ProtocolError`` without changing state if the message is rejected.
        """
        msg = _parse(raw)
        if msg.get("type") == "dismiss":
            self._dismiss(_str_field(msg, "card_id"))
            return None
        if msg.get("type") == "action":
            return self._resolve(msg)
        raise ProtocolError(f"unknown message type: {msg.get('type')!r}")

    def _dismiss(self, card_id: str) -> None:
        card = self._cards.get(card_id)
        if card is None:
            raise ProtocolError(f"no such card: {card_id!r}")
        if card.kind is CardKind.SUGGESTION:
            raise ProtocolError("suggestions are resolved with an action, not dismissed")
        del self._cards[card_id]

    def _resolve(self, msg: dict[str, Any]) -> UserAction:
        suggestion_id = _str_field(msg, "suggestion_id")
        try:
            kind = ActionKind(str(msg.get("kind")))
        except ValueError:
            raise ProtocolError(f"unknown action kind: {msg.get('kind')!r}") from None
        card = self._cards.get(_suggestion_card_id(suggestion_id))
        if card is None:
            raise ProtocolError(f"suggestion {suggestion_id!r} is not pending")

        text: str | None = None
        if kind is ActionKind.EDIT:
            edited = msg.get("text")
            if not isinstance(edited, str) or not edited.strip():
                raise ProtocolError("edit needs a non-empty 'text'")
            text = edited.strip()
            if text == card.text.strip():
                kind = ActionKind.APPROVE  # opened the editor but changed nothing
                text = card.text
        elif kind is ActionKind.APPROVE:
            text = card.text

        del self._cards[card.id]
        return UserAction(suggestion_id=suggestion_id, kind=kind, text=text)


def _parse(raw: str) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message too large")
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"invalid JSON: {e.msg}") from None
    if not isinstance(msg, dict):
        raise ProtocolError("message must be a JSON object")
    return msg


def _str_field(msg: dict[str, Any], name: str) -> str:
    value = msg.get(name)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"missing string field {name!r}")
    return value
