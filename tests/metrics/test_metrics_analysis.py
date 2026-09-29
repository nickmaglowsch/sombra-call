"""Known answers for every metric on the hand-written fixture meetings in ./fixtures."""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sombra.contracts import (
    ActionKind,
    ActionLogged,
    Channel,
    FrameMarker,
    FrameRecord,
    SpeechLine,
    SuggestionLogged,
    TriggerLogged,
    Usage,
    to_json_line,
)
from sombra.metrics import (
    L3Readiness,
    MeetingStats,
    Metric,
    aggregate,
    analyze,
    parse_price_table,
)

FIXTURES = Path(__file__).parent / "fixtures"
PRICES = parse_price_table(
    """
    [models."claude-x"]
    input = 3.0
    output = 15.0
    cache_read = 0.3
    cache_creation = 3.75

    [models."claude-small"]
    input = 1.0
    output = 5.0
    """
)


def values(report_metrics: list[Metric]) -> dict[str, tuple[float | None, str]]:
    return {m.key: (m.value, m.status) for m in report_metrics}


def test_full_meeting_known_answers() -> None:
    report = analyze(FIXTURES / "full", PRICES)
    got = values(report.metrics)

    assert report.name == "full"
    assert report.stats.duration_s == 1800  # 14:30:00 → 15:00:00 in transcript.md
    # latencies 3000, 4000, 5000, 7000, 12000 ms; nearest rank
    assert got["latency_p50_s"] == (5.0, "pass")
    assert got["latency_p95_s"] == (12.0, "fail")
    # approve ×2, edit ×1, discard ×1 → 2/4; not_for_me is not an answer verdict
    assert got["approved_unedited_pct"] == (50.0, "fail")
    # 1 not_for_me in 0.5 h
    assert got["false_triggers_per_h"] == (2.0, "pass")
    # cache_read 3600 / (3600 + 900 + 500)
    assert got["cache_hit_pct"] == (72.0, "fail")
    # claude-x: 500·3 + 250·15 + 3600·0.3 + 900·3.75 = 9705; claude-small: 1000 + 200·5 = 2000
    cost_per_h, status = got["cost_per_h_usd"]
    assert cost_per_h == pytest.approx((9705 + 2000) / 1e6 / 0.5)
    assert status == "pass"
    assert got["frames_per_h"] == (8.0, "n/a")  # 4 kept frames in 0.5 h
    assert got["frames_per_trigger_mean"] == (1.0, "n/a")  # 0+1+0+3+1 over 5
    assert got["frames_per_trigger_max"] == (3.0, "pass")
    assert got["agent_errors"] == (1.0, "n/a")

    s = report.stats
    assert (s.triggers, s.suggestions, s.undecided) == (6, 5, 0)
    assert s.unknown_events == 2  # unknown event type + unknown action kind
    assert s.malformed_lines == 1  # half-written last line
    assert s.unpriced_models == set()


def test_every_metric_shows_target_value_and_status() -> None:
    d = analyze(FIXTURES / "full", PRICES).to_dict()
    for m in d["metrics"]:
        assert set(m) >= {"label", "target", "value", "status"}
    targets = {m["key"]: m["target"] for m in d["metrics"]}
    assert targets["latency_p50_s"] == "<= 6"
    assert targets["approved_unedited_pct"] == ">= 70"
    assert targets["frames_per_h"] is None
    json.dumps(d)  # serialisable


def test_empty_meeting_has_no_values_and_no_failures() -> None:
    report = analyze(FIXTURES / "empty", PRICES)
    got = values(report.metrics)
    assert report.stats.duration_s == 0
    assert got["agent_errors"] == (0.0, "n/a")
    for key, (value, status) in got.items():
        if key != "agent_errors":
            assert value is None, key
            assert status == "n/a", key


def test_meeting_with_only_errors() -> None:
    report = analyze(FIXTURES / "errors_only", PRICES)
    got = values(report.metrics)
    assert report.stats.triggers == 2
    assert report.stats.suggestions == 0
    assert got["agent_errors"] == (2.0, "n/a")
    assert got["latency_p50_s"] == (None, "n/a")
    assert got["approved_unedited_pct"] == (None, "n/a")
    assert got["false_triggers_per_h"] == (0.0, "pass")
    assert got["cost_per_h_usd"] == (0.0, "pass")  # no usage at all costs nothing
    assert report.stats.duration_s == 20 * 60 + 10


def test_cost_unknown_without_price_table_or_for_unpriced_model() -> None:
    report = analyze(FIXTURES / "full")
    assert report.metric("cost_per_h_usd").value is None
    assert report.stats.unpriced_models == {"claude-x", "claude-small"}

    partial = parse_price_table('[models."claude-x"]\ninput = 1.0\n')
    report = analyze(FIXTURES / "full", partial)
    assert report.metric("cost_per_h_usd").status == "n/a"
    assert report.stats.unpriced_models == {"claude-small"}

    fallback = parse_price_table('[models."*"]\ninput = 1.0\n')
    report = analyze(FIXTURES / "full", fallback)
    # all input tokens at US$1/M: 500 + 1000
    assert report.stats.cost_usd == pytest.approx(1500 / 1e6)


def _write_log(meeting: Path, events: list[object]) -> None:
    meeting.mkdir(exist_ok=True)
    (meeting / "log.jsonl").write_text(
        "".join(to_json_line(e) + "\n" for e in events), encoding="utf-8"
    )


T0 = datetime(2026, 9, 29, 14, 0, tzinfo=timezone(timedelta(hours=-3)))


def _suggestion(i: int, at: datetime, **kw: object) -> SuggestionLogged:
    base: dict[str, object] = {
        "suggestion_id": f"s{i}",
        "trigger_id": f"t{i}",
        "ts": at,
        "text": "ok",
        "frames_sent": [],
        "backend": "fake",
        "model": "m",
        "latency_ms": 1000,
    }
    base.update(kw)
    return SuggestionLogged(**base)  # type: ignore[arg-type]


def test_verdict_is_strongest_action(tmp_path: Path) -> None:
    events: list[object] = []
    for i, kinds in enumerate(
        [
            [ActionKind.EDIT, ActionKind.APPROVE],  # edited, then sent → edit
            [ActionKind.DISCARD, ActionKind.NOT_FOR_ME],  # → not_for_me
            [ActionKind.APPROVE],
            [],  # undecided
        ]
    ):
        events.append(_suggestion(i, T0 + timedelta(minutes=i)))
        events += [
            ActionLogged(f"s{i}", T0 + timedelta(minutes=i, seconds=j + 1), k)
            for j, k in enumerate(kinds)
        ]
    _write_log(tmp_path, events)
    s = analyze(tmp_path).stats
    assert dict(s.verdicts) == {"edit": 1, "not_for_me": 1, "approve": 1}
    assert s.undecided == 1
    assert s.answers == 2


def test_naive_timestamps_are_local_time(tmp_path: Path) -> None:
    naive = datetime(2026, 9, 29, 10, 0)
    _write_log(
        tmp_path,
        [
            TriggerLogged("t1", naive, naive, "?", "Nick", 1.0, False),
            TriggerLogged(
                "t2", naive.astimezone() + timedelta(hours=1), naive, "?", "Nick", 1.0, False
            ),
        ],
    )
    assert analyze(tmp_path).stats.duration_s == 3600


def test_aggregate_merges_counts_not_ratios() -> None:
    reports = [analyze(FIXTURES / name, PRICES) for name in ("full", "errors_only", "empty")]
    agg = aggregate(reports)
    assert agg.meetings == ["full", "errors_only", "empty"]
    total = agg.total
    assert total.stats.duration_s == 1800 + 1210
    assert total.stats.agent_errors == 3
    assert total.metric("latency_p50_s").value == 5.0
    assert total.metric("false_triggers_per_h").value == pytest.approx(1 / (3010 / 3600))
    assert total.stats.cost_usd == pytest.approx(11705 / 1e6)
    assert agg.l3 == L3Readiness(answers=4, approved_unedited=2)
    assert not agg.l3.ready
    json.dumps(agg.to_dict())


def test_aggregate_cost_unknown_if_any_meeting_unpriced() -> None:
    agg = aggregate([analyze(FIXTURES / "full", PRICES), analyze(FIXTURES / "full")])
    assert agg.total.stats.cost_usd is None
    assert agg.total.metric("cost_per_h_usd").status == "n/a"


def test_aggregate_of_nothing() -> None:
    agg = aggregate([])
    assert agg.l3.rate_pct is None
    assert not agg.l3.ready


@pytest.mark.parametrize(
    ("answers", "approved", "ready"),
    [(50, 45, True), (49, 49, False), (50, 44, False), (120, 110, True)],
)
def test_l3_readiness(answers: int, approved: int, ready: bool) -> None:
    assert L3Readiness(answers, approved).ready is ready


def test_stats_merge_sums_usage_by_model() -> None:
    a = MeetingStats(usage_by_model={"m": Usage(input_tokens=1)}, cost_usd=1.0)
    b = MeetingStats(usage_by_model={"m": Usage(input_tokens=2), "n": Usage(output_tokens=3)})
    a.merge(b)
    assert a.usage_by_model == {"m": Usage(input_tokens=3), "n": Usage(output_tokens=3)}
    assert a.cost_usd == 1.0


def _one_hour_meeting(root: Path) -> Path:
    """Synthetic 1 h meeting: speech every 2 s, a frame every 10 s, a trigger every minute."""
    meeting = root / "one-hour"
    (meeting / "frames").mkdir(parents=True)
    lines, frames, events = [], [], []
    for sec in range(0, 3601, 2):
        at = T0 + timedelta(seconds=sec)
        ch = Channel.OTHERS if sec % 60 == 0 else Channel.ME
        text = "Nick, o que você acha?" if ch is Channel.OTHERS else "sim, concordo com isso"
        lines.append(SpeechLine(at, ch, text).to_line())
        if sec % 10 == 0:
            n = sec // 10 + 1
            fid = f"f{n:04d}"
            frames.append(
                FrameRecord(fid, at, f"frames/{fid}.jpg", 1280, 800, "zoom.us", "Zoom", 12.0, "ab")
            )
            lines.append(FrameMarker(at, fid, "Zoom").to_line())
        if sec % 60 == 0:
            i = sec // 60
            events.append(TriggerLogged(f"t{i}", at, at, "?", "Nick", 0.9, False))
            events.append(
                _suggestion(
                    i,
                    at + timedelta(seconds=5),
                    latency_ms=4000 + i * 10,
                    frames_sent=["f0001"],
                    usage=Usage(100, 50, 9000, 0),
                )
            )
            events.append(ActionLogged(f"s{i}", at + timedelta(seconds=9), ActionKind.APPROVE))
    (meeting / "transcript.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (meeting / "frames" / "index.jsonl").write_text(
        "".join(to_json_line(f) + "\n" for f in frames), encoding="utf-8"
    )
    _write_log(meeting, events)
    return meeting


def test_one_hour_meeting_reports_in_under_a_second(tmp_path: Path) -> None:
    meeting = _one_hour_meeting(tmp_path)
    start = time.perf_counter()
    report = analyze(meeting, PRICES)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0
    assert report.stats.duration_s == 3609  # last action lands 9 s after the last line
    assert report.stats.suggestions == 61
    assert report.stats.frames_kept == 361
    assert report.metric("approved_unedited_pct").value == 100.0
