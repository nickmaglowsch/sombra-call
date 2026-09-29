"""Render reports as a plain-text table, Markdown or JSON."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from sombra.metrics.analysis import (
    L3_MIN_ANSWERS,
    L3_MIN_UNEDITED_PCT,
    AggregateReport,
    L3Readiness,
    MeetingReport,
    Metric,
)
from sombra.metrics.missed import MissedTrigger

_HEADERS = ("Metric", "Target", "Value", "Status")


def format_value(m: Metric) -> str:
    if m.value is None:
        return "—"
    if m.unit == "US$/h":
        return f"US$ {m.value:.2f}/h"
    if m.unit == "%":
        return f"{m.value:.1f}%"
    if m.unit == "s":
        return f"{m.value:.1f} s"
    if m.unit == "/h":
        return f"{m.value:.1f}/h"
    return f"{m.value:g}"


def format_target(m: Metric) -> str:
    if m.target is None or m.op is None:
        return "—"
    op = "≤" if m.op == "<=" else "≥"
    if m.unit == "US$/h":
        return f"{op} US$ {m.target:g}/h"
    if m.unit in ("%", "/h"):
        return f"{op} {m.target:g}{m.unit}"
    if m.unit == "s":
        return f"{op} {m.target:g} s"
    return f"{op} {m.target:g}"


def _rows(report: MeetingReport) -> list[tuple[str, str, str, str]]:
    return [(m.label, format_target(m), format_value(m), m.status) for m in report.metrics]


def _notes(report: MeetingReport, prefix: str) -> list[str]:
    s = report.stats
    notes = [
        f"duration {s.duration_s / 60:.1f} min · triggers {s.triggers} · "
        f"suggestions {s.suggestions} · undecided {s.undecided}"
    ]
    if s.unpriced_models:
        notes.append("cost unknown: no price for " + ", ".join(sorted(s.unpriced_models)))
    if s.unknown_events or s.malformed_lines:
        notes.append(f"ignored {s.unknown_events} unknown events, {s.malformed_lines} bad lines")
    return [prefix + n for n in notes]


def l3_line(l3: L3Readiness) -> str:
    rate = "—" if l3.rate_pct is None else f"{l3.rate_pct:.1f}%"
    verdict = "ready" if l3.ready else "not ready"
    return (
        f"L3 readiness: {l3.answers}/{L3_MIN_ANSWERS} L2 answers, {rate} approved unedited "
        f"(needs ≥ {L3_MIN_UNEDITED_PCT:g}%) → {verdict}"
    )


def text_report(report: MeetingReport) -> str:
    rows = [_HEADERS, *_rows(report)]
    widths = [max(len(r[i]) for r in rows) for i in range(len(_HEADERS))]
    lines = [f"== {report.name} =="]
    for i, row in enumerate(rows):
        lines.append("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip())
        if i == 0:
            lines.append("  ".join("-" * w for w in widths))
    lines += _notes(report, "")
    return "\n".join(lines)


def markdown_report(report: MeetingReport) -> str:
    lines = [f"### Sombra metrics: {report.name}", ""]
    lines.append("| " + " | ".join(_HEADERS) + " |")
    lines.append("| --- | --- | --- | --- |")
    for label, target, value, status in _rows(report):
        badge = {"pass": "✅ pass", "fail": "❌ fail"}.get(status, status)
        lines.append(f"| {label} | {target} | {value} | {badge} |")
    lines.append("")
    lines += _notes(report, "- ")
    return "\n".join(lines)


def render(
    reports: Sequence[MeetingReport],
    agg: AggregateReport,
    fmt: str,
    missed: dict[str, list[MissedTrigger]] | None = None,
) -> str:
    if fmt == "json":
        data: dict[str, Any] = {
            "meetings": [r.to_dict() for r in reports],
            "aggregate": agg.to_dict(),
        }
        if missed is not None:
            data["missed_triggers"] = {k: [m.to_dict() for m in v] for k, v in missed.items()}
        return json.dumps(data, ensure_ascii=False, indent=2)

    one = markdown_report if fmt == "markdown" else text_report
    parts = [one(r) for r in reports]
    if len(reports) > 1:
        parts.append(one(agg.total))
    parts.append(("**" + l3_line(agg.l3) + "**") if fmt == "markdown" else l3_line(agg.l3))
    if missed is not None:
        parts.append(_missed_block(missed, markdown=fmt == "markdown"))
    return "\n\n".join(parts)


def _missed_block(missed: dict[str, list[MissedTrigger]], *, markdown: bool) -> str:
    title = "Possible missed triggers (label by hand)"
    lines = [f"### {title}" if markdown else f"== {title} =="]
    bullet = "- " if markdown else "  "
    for name, items in missed.items():
        lines.append(f"{bullet}{name}: {len(items)}")
        lines += [f"{bullet}  {m.line}  [alias: {m.alias}]" for m in items]
    return "\n".join(lines)
