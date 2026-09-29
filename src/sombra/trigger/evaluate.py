"""Offline evaluation of a trigger detector on a labelled corpus (JSONL).

Each corpus line is one snippet of timeline, in ``transcript.md`` format, with the
expected outcome::

    {"id": "pos-001", "split": "dev", "kind": "positive",
     "lines": ["[14:00:00] OUTROS: Nick, o que você acha?"],
     "trigger": true, "needs_screen": false}

Optional keys: ``name``/``aliases`` (the user; default "Nick" / "Nicolas"), ``needs_screen`` (only
checked on true positives), ``frames`` (expected ``candidate_frames``) and ``note``.
Every snippet gets a fresh detector; a snippet is predicted positive when any of its
lines fires.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sombra.contracts import TriggerDetector, TriggerEvent, parse_line
from sombra.trigger.detector import NameTriggerDetector

DEFAULT_NAME = "Nick"
DEFAULT_ALIASES = ("Nicolas",)
_DAY = datetime(2026, 9, 29, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class CorpusCase:
    id: str
    split: str  # "dev" or "holdout"
    kind: str
    lines: tuple[str, ...]
    trigger: bool
    name: str = DEFAULT_NAME
    aliases: tuple[str, ...] = DEFAULT_ALIASES
    needs_screen: bool | None = None
    frames: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class EvalResult:
    tp: int
    fp: int
    fn: int
    tn: int
    screen_errors: int  # true positives whose needs_screen/frames differ from the label
    mistakes: tuple[str, ...] = field(default=())  # "<id>: <what went wrong>"

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0


def load_corpus(path: Path) -> list[CorpusCase]:
    cases = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        d = json.loads(raw)
        try:
            frames = d.get("frames")
            cases.append(
                CorpusCase(
                    id=d["id"],
                    split=d["split"],
                    kind=d["kind"],
                    lines=tuple(d["lines"]),
                    trigger=bool(d["trigger"]),
                    name=d.get("name", DEFAULT_NAME),
                    aliases=tuple(d.get("aliases", DEFAULT_ALIASES)),
                    needs_screen=d.get("needs_screen"),
                    frames=tuple(frames) if frames is not None else None,
                )
            )
        except KeyError as e:
            raise ValueError(f"{path}:{n}: missing key {e}") from e
    return cases


def default_factory(case: CorpusCase) -> TriggerDetector:
    return NameTriggerDetector(case.name, case.aliases)


def run_case(case: CorpusCase, detector: TriggerDetector) -> list[TriggerEvent]:
    events = []
    for line in case.lines:
        if (event := detector.feed(parse_line(line, day=_DAY))) is not None:
            events.append(event)
    return events


def evaluate(
    cases: Iterable[CorpusCase],
    factory: Callable[[CorpusCase], TriggerDetector] = default_factory,
) -> EvalResult:
    tp = fp = fn = tn = screen_errors = 0
    mistakes: list[str] = []
    for case in cases:
        events = run_case(case, factory(case))
        fired = bool(events)
        if fired and case.trigger:
            tp += 1
            if (problem := _screen_problem(case, events[0])) is not None:
                screen_errors += 1
                mistakes.append(f"{case.id}: {problem}")
        elif fired:
            fp += 1
            mistakes.append(f"{case.id}: false trigger on {events[0].question!r}")
        elif case.trigger:
            fn += 1
            mistakes.append(f"{case.id}: missed")
        else:
            tn += 1
    return EvalResult(tp, fp, fn, tn, screen_errors, tuple(mistakes))


def _screen_problem(case: CorpusCase, event: TriggerEvent) -> str | None:
    if case.needs_screen is not None and event.needs_screen != case.needs_screen:
        return f"needs_screen={event.needs_screen}, expected {case.needs_screen}"
    if case.frames is not None and tuple(event.candidate_frames) != case.frames:
        return f"frames={list(event.candidate_frames)}, expected {list(case.frames)}"
    return None


def split(cases: Sequence[CorpusCase], name: str) -> list[CorpusCase]:
    return [c for c in cases if c.split == name]
