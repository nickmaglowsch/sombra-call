"""``NameTriggerDetector``: decides, per timeline entry, whether the user was just called.

Implements ``contracts.TriggerDetector`` (G1-G5):

* G1: fuzzy match of the name/aliases, on ``OUTROS`` lines only.
* G2: the utterance must also ask something (question mark, interrogative, request
  verb, opinion) and the name must sit in a vocative position; a mention alone
  ("falei com o Nick ontem") never fires. A name and its question may arrive on two
  consecutive ``OUTROS`` lines up to ``split_s`` apart, in either order.
* G3: ``window`` holds every entry of the last ``window_s`` seconds (ring buffer).
* G4: deictic words set ``needs_screen``; ``candidate_frames`` is then the frame on
  screen plus up to two earlier distinct frames, current first (empty otherwise).
* G5: no new trigger within ``cooldown_s`` of the last one, and a near-identical
  question within ``dedupe_s`` is dropped.
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from rapidfuzz import fuzz

from sombra.contracts import Channel, FrameMarker, SpeechLine, TimelineEntry, TriggerEvent
from sombra.trigger.names import NameHit, NameMatcher
from sombra.trigger.signals import (
    Position,
    addresses_someone_else,
    is_deictic,
    is_phatic,
    name_position,
    request_strength,
)
from sombra.trigger.text import fold, tokenize

MAX_FRAMES = 3


@dataclass(frozen=True, slots=True)
class TriggerSettings:
    """Tuning knobs. Defaults are what the corpus test in ``tests/trigger`` asserts on."""

    threshold: float = 0.7  # minimum score to fire
    min_request: float = 0.4  # request structure needed at all (name alone never fires)
    window_s: float = 60.0  # G3 context window
    split_s: float = 5.0  # max gap between a name line and its question line
    cooldown_s: float = 30.0  # G5
    dedupe_s: float = 120.0  # G5: how long a fired question blocks near-identical ones
    dedupe_ratio: float = 90.0  # rapidfuzz ratio (0..100) above which questions are "the same"
    w_name: float = 0.45
    w_vocative: float = 0.25
    w_request: float = 0.30


@dataclass(frozen=True, slots=True)
class _Analysis:
    line: SpeechLine
    hit: NameHit | None
    position: Position | None
    request: float
    phatic: bool
    other_addressee: bool  # opens by calling someone else ("Maria, ...")

    @property
    def calls_user(self) -> bool:
        return self.position is not None and not self.position.reference


class NameTriggerDetector:
    """Rule-based trigger detector for one meeting. Not thread-safe; feed entries in order."""

    def __init__(
        self,
        name: str,
        aliases: Sequence[str] = (),
        settings: TriggerSettings | None = None,
    ) -> None:
        self.settings = settings or TriggerSettings()
        self._matcher = NameMatcher([name, *aliases])
        self._buffer: deque[TimelineEntry] = deque()
        self._frames: deque[str] = deque(maxlen=MAX_FRAMES)  # distinct frame ids, newest last
        self._pending_name: _Analysis | None = None  # vocative without a question yet
        self._pending_question: _Analysis | None = None  # question without a name yet
        self._last_fired: datetime | None = None
        self._recent_questions: deque[tuple[datetime, str]] = deque()

    # --- TriggerDetector ----------------------------------------------------------

    def feed(self, entry: TimelineEntry) -> TriggerEvent | None:
        self._remember(entry)
        if isinstance(entry, FrameMarker):
            if not self._frames or self._frames[-1] != entry.frame_id:
                if entry.frame_id in self._frames:
                    self._frames.remove(entry.frame_id)
                self._frames.append(entry.frame_id)
            return None
        if entry.channel is not Channel.OTHERS:
            return None  # G1: the user's own speech never triggers
        return self._on_others(entry)

    # --- internals ----------------------------------------------------------------

    def _remember(self, entry: TimelineEntry) -> None:
        self._buffer.append(entry)
        horizon = entry.ts - timedelta(seconds=self.settings.window_s)
        while self._buffer and self._buffer[0].ts < horizon:
            self._buffer.popleft()

    def _analyse(self, line: SpeechLine) -> _Analysis:
        tokens = tokenize(line.text)
        hits = self._matcher.find_all(tokens)
        best: tuple[NameHit, Position] | None = None
        for hit in hits:
            pos = name_position(tokens, hit)
            if best is None or _rank(hit, pos) > _rank(*best):
                best = (hit, pos)
        return _Analysis(
            line=line,
            hit=best[0] if best else None,
            position=best[1] if best else None,
            request=request_strength(line.text),
            phatic=is_phatic(tokens, best[0] if best else None),
            other_addressee=addresses_someone_else(tokens),
        )

    def _within_split(self, earlier: _Analysis | None, now: datetime) -> bool:
        if earlier is None:
            return False
        return (now - earlier.line.ts).total_seconds() <= self.settings.split_s

    def _on_others(self, line: SpeechLine) -> TriggerEvent | None:
        s = self.settings
        a = self._analyse(line)
        pending_name = self._pending_name
        if not self._within_split(pending_name, line.ts):
            pending_name = None
        pending_q = self._pending_question
        if not self._within_split(pending_q, line.ts):
            pending_q = None
        self._pending_name = None
        self._pending_question = None

        if a.hit is not None and a.calls_user:
            if pending_q is not None and _is_bare_vocative(a):
                # "O que você acha dessa proposta?" / "Nick?"
                return self._fire([pending_q, a], a, max(pending_q.request, a.request))
            if a.request >= s.min_request and not a.phatic:
                return self._fire([a], a, a.request)
            if a.request < s.min_request:
                self._pending_name = a
            return None
        if a.hit is None:
            asks = a.request >= s.min_request and not a.phatic
            if pending_name is not None and asks and not a.other_addressee:
                # "Nick..." / "o que você acha desse número?"
                return self._fire([pending_name, a], pending_name, a.request)
            if asks:
                self._pending_question = a
        return None

    def _fire(
        self, parts: list[_Analysis], named: _Analysis, request: float
    ) -> TriggerEvent | None:
        s = self.settings
        assert named.hit is not None and named.position is not None  # noqa: S101 - internal invariant
        score = (
            s.w_name * named.hit.similarity
            + s.w_vocative * named.position.vocative
            + s.w_request * request
        )
        if score < s.threshold:
            return None
        ts = parts[-1].line.ts
        question = " ".join(" ".join(p.line.text.split()) for p in parts)
        if self._suppressed(ts, question):
            return None
        self._last_fired = ts
        self._recent_questions.append((ts, fold(question)))
        needs_screen = is_deictic(question)
        return TriggerEvent(
            id=uuid.uuid4().hex,
            ts=ts,
            question=question,
            matched_alias=named.hit.alias,
            score=round(min(1.0, score), 3),
            window=tuple(self._buffer),
            needs_screen=needs_screen,
            candidate_frames=tuple(reversed(self._frames)) if needs_screen else (),
        )

    def _suppressed(self, ts: datetime, question: str) -> bool:
        s = self.settings
        if self._last_fired is not None and (ts - self._last_fired).total_seconds() < s.cooldown_s:
            return True
        while self._recent_questions and (
            (ts - self._recent_questions[0][0]).total_seconds() > s.dedupe_s
        ):
            self._recent_questions.popleft()
        folded = fold(question)
        return any(
            fuzz.ratio(folded, q, score_cutoff=s.dedupe_ratio) for _, q in self._recent_questions
        )


def _rank(hit: NameHit, pos: Position) -> tuple[bool, float, float]:
    return (not pos.reference, pos.vocative, hit.similarity)


def _is_bare_vocative(a: _Analysis) -> bool:
    """A line that is essentially just the name: "Nick?", "né, Nick?", "e aí, Nique?"."""
    assert a.hit is not None  # noqa: S101 - caller checks
    words = [t for t in tokenize(a.line.text) if t.is_word]
    return len(words) - (a.hit.end - a.hit.start) <= 2
