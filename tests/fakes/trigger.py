"""``TriggerDetector`` fake: fires on scripted phrases instead of fuzzy name matching."""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from sombra.contracts import FrameMarker, SpeechLine, TimelineEntry, TriggerEvent

WINDOW = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class TriggerRule:
    """Fire when a speech line contains ``contains`` (case-insensitive).

    With ``needs_screen`` and no explicit ``candidate_frames``, the candidates are
    the last three frame markers fed, most recent first (what G4 does).
    """

    contains: str
    needs_screen: bool = False
    candidate_frames: Sequence[str] | None = None
    alias: str = "Nick"
    score: float = 0.9


class ScriptedTriggerDetector:
    """Fires on the rules, in order; the first matching rule wins.

    ``TriggerEvent.ts`` is the speech line's ``ts``. The contract means the *end*
    of the utterance; a scripted line has no duration, so here the two coincide.
    """

    def __init__(self, rules: Iterable[TriggerRule]) -> None:
        self.rules = list(rules)
        self.fed: list[TimelineEntry] = []
        self.fired: list[TriggerEvent] = []
        self.fired_at: list[float] = []  # time.perf_counter() when each event was returned
        self._frames: list[str] = []

    def feed(self, entry: TimelineEntry) -> TriggerEvent | None:
        self.fed.append(entry)
        if isinstance(entry, FrameMarker):
            self._frames.append(entry.frame_id)
            return None
        rule = self._match(entry)
        if rule is None:
            return None
        frames: Sequence[str] = ()
        if rule.needs_screen:
            frames = (
                tuple(rule.candidate_frames)
                if rule.candidate_frames is not None
                else tuple(reversed(self._frames[-3:]))
            )
        event = TriggerEvent(
            id=f"t{len(self.fired) + 1:03d}",
            ts=entry.ts,
            question=entry.text,
            matched_alias=rule.alias,
            score=rule.score,
            window=tuple(e for e in self.fed if entry.ts - e.ts <= WINDOW),
            needs_screen=rule.needs_screen,
            candidate_frames=frames,
        )
        self.fired.append(event)
        self.fired_at.append(time.perf_counter())
        return event

    def _match(self, entry: SpeechLine) -> TriggerRule | None:
        text = entry.text.casefold()
        for rule in self.rules:
            if rule.contains.casefold() in text:
                return rule
        return None
