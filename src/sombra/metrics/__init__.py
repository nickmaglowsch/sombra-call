"""Meeting metrics: turn a meeting folder into the PRD's success-metric table.

Owns the "Métricas de sucesso" report and the L3 release criterion (≥ 50 L2 answers,
≥ 90% approved unedited). Reads ``log.jsonl``, ``frames/index.jsonl`` and
``transcript.md`` through the ``sombra.contracts`` records and parsers; never writes
to the meeting folder. CPU usage and STT name error rate are measured outside this
package (see ``docs/metrics.md``).
"""

from sombra.metrics.analysis import (
    TARGETS,
    AggregateReport,
    L3Readiness,
    MeetingReport,
    MeetingStats,
    Metric,
    aggregate,
    analyze,
)
from sombra.metrics.missed import MissedTrigger, missed_triggers
from sombra.metrics.prices import ModelPrice, PriceTable, load_price_table, parse_price_table

__all__ = [
    "TARGETS",
    "AggregateReport",
    "L3Readiness",
    "MeetingReport",
    "MeetingStats",
    "Metric",
    "MissedTrigger",
    "ModelPrice",
    "PriceTable",
    "aggregate",
    "analyze",
    "load_price_table",
    "missed_triggers",
    "parse_price_table",
]
