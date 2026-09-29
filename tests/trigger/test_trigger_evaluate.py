import json
from pathlib import Path

import pytest

from sombra.contracts import TimelineEntry, TriggerEvent
from sombra.trigger.evaluate import CorpusCase, EvalResult, evaluate, load_corpus


def _write(tmp_path: Path, records: list[dict[str, object]]) -> Path:
    path = tmp_path / "corpus.jsonl"
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    path.write_text(body + "\n\n", encoding="utf-8")
    return path


def test_load_corpus_defaults_and_overrides(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [
            {"id": "a", "split": "dev", "kind": "positive", "lines": ["x"], "trigger": True},
            {
                "id": "b",
                "split": "holdout",
                "kind": "misspelling",
                "lines": ["y"],
                "trigger": False,
                "name": "Thiago",
                "aliases": ["Thi"],
                "needs_screen": True,
                "frames": ["f0001"],
            },
        ],
    )
    a, b = load_corpus(path)
    assert (a.name, a.aliases, a.needs_screen, a.frames) == ("Nick", ("Nicolas",), None, None)
    assert (b.name, b.aliases, b.needs_screen, b.frames) == ("Thiago", ("Thi",), True, ("f0001",))


def test_load_corpus_reports_missing_keys(tmp_path: Path) -> None:
    path = _write(tmp_path, [{"id": "a", "split": "dev", "kind": "positive", "lines": []}])
    with pytest.raises(ValueError, match=r"corpus.jsonl:1: missing key 'trigger'"):
        load_corpus(path)


def test_empty_result_is_perfect() -> None:
    result = EvalResult(0, 0, 0, 0, 0)
    assert result.precision == 1.0
    assert result.recall == 1.0


class _Always:
    """Fake detector: fires on every entry with a fixed screen decision."""

    def __init__(self, needs_screen: bool) -> None:
        self.needs_screen = needs_screen

    def feed(self, entry: TimelineEntry) -> TriggerEvent:
        return TriggerEvent("t", entry.ts, "q", "Nick", 1.0, (), self.needs_screen, ("f0001",))


class _Never:
    def feed(self, entry: TimelineEntry) -> None:
        return None


LINE = "[14:00:00] OUTROS: Nick, e aí?"


def _case(trigger: bool, **kw: object) -> CorpusCase:
    return CorpusCase("c", "dev", "positive", (LINE,), trigger, **kw)  # type: ignore[arg-type]


def test_counts_and_mistakes() -> None:
    cases = [_case(True), _case(False)]
    fired = evaluate(cases, lambda _: _Always(False))
    assert (fired.tp, fired.fp, fired.fn, fired.tn) == (1, 1, 0, 0)
    assert fired.precision == 0.5
    assert fired.mistakes == ("c: false trigger on 'q'",)
    silent = evaluate(cases, lambda _: _Never())
    assert (silent.tp, silent.fp, silent.fn, silent.tn) == (0, 0, 1, 1)
    assert silent.recall == 0.0
    assert silent.mistakes == ("c: missed",)


def test_screen_errors() -> None:
    wrong_flag = evaluate([_case(True, needs_screen=True)], lambda _: _Always(False))
    assert wrong_flag.screen_errors == 1
    assert "needs_screen=False" in wrong_flag.mistakes[0]
    wrong_frames = evaluate([_case(True, frames=("f0002",))], lambda _: _Always(True))
    assert wrong_frames.screen_errors == 1
    assert "frames=['f0001']" in wrong_frames.mistakes[0]
    ok = evaluate([_case(True, needs_screen=True, frames=("f0001",))], lambda _: _Always(True))
    assert ok.screen_errors == 0
