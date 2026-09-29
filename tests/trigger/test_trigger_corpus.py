"""Acceptance gate: precision and recall on the labelled PT-BR corpus.

``tests/fixtures/trigger/corpus.jsonl`` has a ``dev`` split (used while writing the
rules) and a ``holdout`` split (~30%, written separately and not tuned on). The
targets hold on each split, so a regression on unseen phrasing fails CI too.
"""

from collections import Counter
from pathlib import Path

import pytest

from sombra.trigger import NameTriggerDetector, evaluate, load_corpus
from sombra.trigger.evaluate import CorpusCase, run_case, split

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "trigger" / "corpus.jsonl"
TARGET = 0.9
CASES = load_corpus(CORPUS)


def test_corpus_shape() -> None:
    assert len(CASES) >= 150
    assert len({c.id for c in CASES}) == len(CASES)
    kinds = Counter(c.kind for c in CASES)
    for kind in ("positive", "near_miss", "name_only", "split", "misspelling", "mixed", "eu"):
        assert kinds[kind] >= 5, kind
    holdout = len(split(CASES, "holdout")) / len(CASES)
    assert 0.25 <= holdout <= 0.4
    assert {c.split for c in CASES} == {"dev", "holdout"}
    assert sum(c.trigger for c in CASES) >= 60
    assert sum(not c.trigger for c in CASES) >= 60


@pytest.mark.parametrize("name", ["dev", "holdout", "all"])
def test_precision_and_recall(name: str) -> None:
    cases = CASES if name == "all" else split(CASES, name)
    result = evaluate(cases)
    report = "\n".join(result.mistakes)
    assert result.precision >= TARGET, report
    assert result.recall >= TARGET, report


def test_deictic_cases_pick_the_right_frames() -> None:
    labelled = [c for c in CASES if c.needs_screen is not None or c.frames is not None]
    assert len(labelled) >= 10
    assert evaluate(labelled).screen_errors == 0


def test_candidate_frames_are_capped_and_current_first() -> None:
    for case in CASES:
        frames_seen = [ln.split()[2] for ln in case.lines if " TELA " in ln]
        for event in run_case(case, NameTriggerDetector(case.name, case.aliases)):
            assert len(event.candidate_frames) <= 3
            if event.candidate_frames:
                assert event.candidate_frames[0] == frames_seen[-1], case.id


def _as_eu(case: CorpusCase) -> CorpusCase:
    lines = tuple(ln.replace("] OUTROS: ", "] EU: ") for ln in case.lines)
    return CorpusCase(case.id, case.split, case.kind, lines, trigger=False, name=case.name)


def test_eu_lines_never_trigger_on_the_whole_corpus() -> None:
    rewritten = [_as_eu(c) for c in CASES]
    assert all("OUTROS" not in ln for c in rewritten for ln in c.lines)
    result = evaluate(rewritten)
    assert result.fp == 0, result.mistakes


def test_name_only_cases_never_trigger() -> None:
    result = evaluate(c for c in CASES if c.kind == "name_only")
    assert result.fp == 0, result.mistakes
