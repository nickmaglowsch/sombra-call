"""Success metrics for one meeting (``analyze``) and across meetings (``aggregate``).

Definitions (also in ``docs/metrics.md``):

- **Latency**: ``SuggestionLogged.latency_ms`` (end of question → shown in overlay),
  nearest-rank p50 / p95.
- **Verdict** of a suggestion: the strongest of its logged actions, in the order
  ``not_for_me`` > ``edit`` > ``approve`` > ``discard`` (an edit followed by an approve is
  an edit). Suggestions with no action are "undecided" and count in no ratio.
- **Approved without edit**: of suggestions whose verdict is approve, edit or discard, the
  share approved. ``not_for_me`` is a false trigger, not an answer verdict.
- **False triggers / h**: suggestions whose verdict is ``not_for_me``, per hour.
- **Frames / h**: kept (post-dedupe) frames in ``frames/index.jsonl``, per hour.
- **Frames per trigger**: ``len(frames_sent)`` of each suggestion; mean and max.
- **Cache hit rate**: ``cache_read / (cache_read + cache_creation + input)`` over the
  answer calls (suggestions); summary calls are a different prompt.
- **Cost / h**: every logged ``Usage`` (suggestions and summary epochs) priced with the
  price table, per hour. Unknown when there is no table or a model is not in it.
- **Duration**: first to last timestamp seen in the log, frame index and transcript.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from sombra.contracts import (
    ActionKind,
    ActionLogged,
    AgentErrorLogged,
    SuggestionLogged,
    SummaryEpochLogged,
    TriggerLogged,
    Usage,
)
from sombra.metrics.prices import PriceTable
from sombra.metrics.reader import aware, meeting_day, read_frames, read_log, read_transcript

Op = Literal["<=", ">="]

# key -> (label, unit, target, op). Targets are the PRD's MVP success table.
TARGETS: dict[str, tuple[str, str, float | None, Op | None]] = {
    "latency_p50_s": ("Latency p50 (question → overlay)", "s", 6.0, "<="),
    "latency_p95_s": ("Latency p95 (question → overlay)", "s", 10.0, "<="),
    "approved_unedited_pct": ("Answers approved without edits", "%", 70.0, ">="),
    "false_triggers_per_h": ("False triggers (not_for_me)", "/h", 2.0, "<="),
    "cache_hit_pct": ("Prompt-prefix cache hit rate", "%", 80.0, ">="),
    "cost_per_h_usd": ("API cost", "US$/h", 1.0, "<="),
    "frames_per_h": ("Frames kept after dedupe", "/h", None, None),
    "frames_per_trigger_mean": ("Frames attached per trigger (mean)", "", None, None),
    "frames_per_trigger_max": ("Frames attached per trigger (max)", "", 3.0, "<="),
    "agent_errors": ("Agent errors", "", None, None),
}

# Strongest first; see "Verdict" above.
_VERDICT_ORDER = (ActionKind.NOT_FOR_ME, ActionKind.EDIT, ActionKind.APPROVE, ActionKind.DISCARD)

L3_MIN_ANSWERS = 50
L3_MIN_UNEDITED_PCT = 90.0


@dataclass(frozen=True, slots=True)
class Metric:
    key: str
    label: str
    value: float | None
    unit: str
    target: float | None
    op: Op | None

    @property
    def status(self) -> Literal["pass", "fail", "n/a"]:
        if self.value is None or self.target is None or self.op is None:
            return "n/a"
        ok = self.value <= self.target if self.op == "<=" else self.value >= self.target
        return "pass" if ok else "fail"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "target": None if self.target is None else f"{self.op} {self.target:g}",
            "status": self.status,
        }


@dataclass(slots=True)
class MeetingStats:
    """Raw counts behind the metrics; aggregating merges these, never averages ratios."""

    duration_s: float = 0.0
    triggers: int = 0
    suggestions: int = 0
    latencies_ms: list[int] = field(default_factory=list)
    frames_per_suggestion: list[int] = field(default_factory=list)
    verdicts: Counter[str] = field(default_factory=Counter)
    undecided: int = 0
    frames_kept: int = 0
    answer_usage: Usage = field(default_factory=Usage)
    usage_by_model: dict[str, Usage] = field(default_factory=dict)
    cost_usd: float | None = 0.0
    unpriced_models: set[str] = field(default_factory=set)
    agent_errors: int = 0
    unknown_events: int = 0
    malformed_lines: int = 0

    @property
    def answers(self) -> int:
        """Suggestions the user judged as answers: approve, edit or discard."""
        verdicts = (ActionKind.APPROVE, ActionKind.EDIT, ActionKind.DISCARD)
        return sum(self.verdicts[k] for k in verdicts)

    def merge(self, other: MeetingStats) -> None:
        self.duration_s += other.duration_s
        self.triggers += other.triggers
        self.suggestions += other.suggestions
        self.latencies_ms += other.latencies_ms
        self.frames_per_suggestion += other.frames_per_suggestion
        self.verdicts.update(other.verdicts)
        self.undecided += other.undecided
        self.frames_kept += other.frames_kept
        self.answer_usage = _add(self.answer_usage, other.answer_usage)
        for model, usage in other.usage_by_model.items():
            self.usage_by_model[model] = _add(self.usage_by_model.get(model, Usage()), usage)
        if self.cost_usd is None or other.cost_usd is None:
            self.cost_usd = None
        else:
            self.cost_usd += other.cost_usd
        self.unpriced_models |= other.unpriced_models
        self.agent_errors += other.agent_errors
        self.unknown_events += other.unknown_events
        self.malformed_lines += other.malformed_lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_s": self.duration_s,
            "triggers": self.triggers,
            "suggestions": self.suggestions,
            "verdicts": {str(k): v for k, v in sorted(self.verdicts.items())},
            "undecided": self.undecided,
            "frames_kept": self.frames_kept,
            "answer_usage": _usage_dict(self.answer_usage),
            "usage_by_model": {m: _usage_dict(u) for m, u in sorted(self.usage_by_model.items())},
            "cost_usd": self.cost_usd,
            "unpriced_models": sorted(self.unpriced_models),
            "agent_errors": self.agent_errors,
            "unknown_events": self.unknown_events,
            "malformed_lines": self.malformed_lines,
        }


@dataclass(frozen=True, slots=True)
class MeetingReport:
    name: str
    stats: MeetingStats
    metrics: list[Metric]

    def metric(self, key: str) -> Metric:
        return next(m for m in self.metrics if m.key == key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "metrics": [m.to_dict() for m in self.metrics],
            "stats": self.stats.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class L3Readiness:
    """PRD L3 gate: ≥ 50 L2 answers with ≥ 90% approved unedited."""

    answers: int
    approved_unedited: int

    @property
    def rate_pct(self) -> float | None:
        return None if self.answers == 0 else 100.0 * self.approved_unedited / self.answers

    @property
    def ready(self) -> bool:
        rate = self.rate_pct
        return self.answers >= L3_MIN_ANSWERS and rate is not None and rate >= L3_MIN_UNEDITED_PCT

    def to_dict(self) -> dict[str, Any]:
        return {
            "answers": self.answers,
            "approved_unedited": self.approved_unedited,
            "rate_pct": self.rate_pct,
            "ready": self.ready,
            "min_answers": L3_MIN_ANSWERS,
            "min_rate_pct": L3_MIN_UNEDITED_PCT,
        }


@dataclass(frozen=True, slots=True)
class AggregateReport:
    meetings: list[str]
    total: MeetingReport
    l3: L3Readiness

    def to_dict(self) -> dict[str, Any]:
        return {"meetings": self.meetings, "total": self.total.to_dict(), "l3": self.l3.to_dict()}


def analyze(meeting_dir: Path, prices: PriceTable | None = None) -> MeetingReport:
    """Read one meeting folder (read-only) and compute its success metrics."""
    log = read_log(meeting_dir)
    frames, bad_frames = read_frames(meeting_dir)
    stats = MeetingStats(
        frames_kept=len(frames),
        unknown_events=log.unknown,
        malformed_lines=log.malformed + bad_frames,
    )

    stamps = [e.ts for e in log.events] + [f.ts for f in frames]
    day = meeting_day(meeting_dir, stamps)
    stamps += [e.ts for e in read_transcript(meeting_dir, day)]
    stats.duration_s = _span_s(stamps)

    suggestions: dict[str, SuggestionLogged] = {}
    actions: dict[str, set[ActionKind]] = {}
    for event in log.events:
        if isinstance(event, TriggerLogged):
            stats.triggers += 1
        elif isinstance(event, SuggestionLogged):
            suggestions[event.suggestion_id] = event
            stats.answer_usage = _add(stats.answer_usage, event.usage)
            _add_model_usage(stats, event.model, event.usage)
        elif isinstance(event, SummaryEpochLogged):
            _add_model_usage(stats, event.model, event.usage)
        elif isinstance(event, ActionLogged):
            actions.setdefault(event.suggestion_id, set()).add(event.kind)
        elif isinstance(event, AgentErrorLogged):
            stats.agent_errors += 1

    stats.suggestions = len(suggestions)
    stats.latencies_ms = [s.latency_ms for s in suggestions.values()]
    stats.frames_per_suggestion = [len(s.frames_sent) for s in suggestions.values()]
    for sid in suggestions:
        kinds = actions.get(sid, set())
        verdict = next((k for k in _VERDICT_ORDER if k in kinds), None)
        if verdict is None:
            stats.undecided += 1
        else:
            stats.verdicts[verdict] += 1

    stats.cost_usd, stats.unpriced_models = _cost(stats.usage_by_model, prices)
    return MeetingReport(meeting_dir.name, stats, compute_metrics(stats))


def aggregate(reports: Iterable[MeetingReport]) -> AggregateReport:
    """Merge meetings by summing raw counts, then recompute every metric + the L3 line."""
    reports = list(reports)
    total = MeetingStats()
    for r in reports:
        total.merge(r.stats)
    l3 = L3Readiness(total.answers, total.verdicts[ActionKind.APPROVE])
    return AggregateReport(
        [r.name for r in reports], MeetingReport("all", total, compute_metrics(total)), l3
    )


def compute_metrics(stats: MeetingStats) -> list[Metric]:
    hours = stats.duration_s / 3600

    def per_hour(n: float | None) -> float | None:
        return None if n is None or hours <= 0 else n / hours

    u = stats.answer_usage
    prompt = u.cache_read_input_tokens + u.cache_creation_input_tokens + u.input_tokens
    lat = sorted(stats.latencies_ms)
    fps = stats.frames_per_suggestion
    values: dict[str, float | None] = {
        "latency_p50_s": _ms_to_s(_percentile(lat, 50)),
        "latency_p95_s": _ms_to_s(_percentile(lat, 95)),
        "approved_unedited_pct": _pct(stats.verdicts[ActionKind.APPROVE], stats.answers),
        "false_triggers_per_h": per_hour(stats.verdicts[ActionKind.NOT_FOR_ME]),
        "cache_hit_pct": _pct(u.cache_read_input_tokens, prompt),
        "cost_per_h_usd": per_hour(stats.cost_usd),
        "frames_per_h": per_hour(stats.frames_kept),
        "frames_per_trigger_mean": sum(fps) / len(fps) if fps else None,
        "frames_per_trigger_max": float(max(fps)) if fps else None,
        "agent_errors": float(stats.agent_errors),
    }
    return [
        Metric(key, label, values[key], unit, target, op)
        for key, (label, unit, target, op) in TARGETS.items()
    ]


def _percentile(sorted_values: Sequence[int], p: float) -> int | None:
    """Nearest-rank percentile: the smallest value with at least p% of values <= it."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(p / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def _ms_to_s(ms: int | None) -> float | None:
    return None if ms is None else ms / 1000


def _pct(num: int, den: int) -> float | None:
    return None if den == 0 else 100.0 * num / den


def _span_s(stamps: Sequence[datetime]) -> float:
    if not stamps:
        return 0.0
    ts = [aware(t) for t in stamps]
    return (max(ts) - min(ts)).total_seconds()


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_input_tokens=a.cache_read_input_tokens + b.cache_read_input_tokens,
        cache_creation_input_tokens=a.cache_creation_input_tokens + b.cache_creation_input_tokens,
    )


def _add_model_usage(stats: MeetingStats, model: str, usage: Usage) -> None:
    stats.usage_by_model[model] = _add(stats.usage_by_model.get(model, Usage()), usage)


def _cost(
    usage_by_model: dict[str, Usage], prices: PriceTable | None
) -> tuple[float | None, set[str]]:
    total = 0.0
    unpriced: set[str] = set()
    for model, usage in usage_by_model.items():
        if usage == Usage():
            continue
        price = prices.price_for(model) if prices is not None else None
        if price is None:
            unpriced.add(model)
        else:
            total += price.cost(usage)
    return (None if unpriced else total), unpriced


def _usage_dict(u: Usage) -> dict[str, int]:
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_input_tokens": u.cache_read_input_tokens,
        "cache_creation_input_tokens": u.cache_creation_input_tokens,
    }
