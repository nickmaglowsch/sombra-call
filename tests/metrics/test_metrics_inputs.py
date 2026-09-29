"""Reading the meeting folder (forward compatibility), the price table and missed triggers."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sombra.contracts import ActionKind, ActionLogged, SuggestionLogged, TriggerLogged, Usage
from sombra.metrics import ModelPrice, load_price_table, missed_triggers, parse_price_table
from sombra.metrics.reader import meeting_day, parse_event, read_frames, read_log

FIXTURES = Path(__file__).parent / "fixtures"
BRT = timezone(timedelta(hours=-3))

# --- reader --------------------------------------------------------------------------


def test_log_ignores_unknown_types_and_keys_and_skips_bad_lines(tmp_path: Path) -> None:
    (tmp_path / "log.jsonl").write_text(
        "\n".join(
            [
                '{"type":"trigger","trigger_id":"t1","ts":"2026-09-29T14:00:00-03:00",'
                '"detected_at":"2026-09-29T14:00:01-03:00","question":"?","matched_alias":"Nick",'
                '"score":0.9,"needs_screen":false,"classifier":"laya-v1","extra":[1,2]}',
                '{"type":"brand_new_event","anything":true}',
                '{"no_type":1}',
                '{"type":"action","suggestion_id":"s1","ts":"2026-09-29T14:00:05-03:00",'
                '"kind":"super_approve"}',
                "",
                "[1, 2, 3]",
                "not json at all",
                '{"type":"suggestion","suggestion_id":"s1"}',
                '{"type":"suggestion","suggestion_id":"s1","trigger_id":"t1",'
                '"ts":"2026-09-29T14:00:04-03:00","text":"x","frames_sent":"f0001",'
                '"backend":"b","model":"m","latency_ms":10}',
                '{"type":"suggestion","suggestion_id":"s1","trigger_id":"t1",'
                '"ts":"2026-09-29T14:00:04-03:00","text":"x","frames_sent":[],'
                '"backend":"b","model":"m","latency_ms":"10"}',
                '{"type":"suggestion","suggestion_id":"s1","trigger_id":"t1",'
                '"ts":"2026-09-29T14:00:04-03:00","text":"x","frames_sent":[],'
                '"backend":"b","model":"m","latency_ms":10,"usage":[]}',
                '{"type":"trigger","trigger_id":"t1","ts":12}',
            ]
        ),
        encoding="utf-8",
    )
    log = read_log(tmp_path)
    assert [type(e) for e in log.events] == [TriggerLogged]
    assert log.unknown == 3  # new type, missing type, new action kind
    assert log.malformed == 7


def test_parse_event_builds_contract_records() -> None:
    event = parse_event(
        {
            "type": "suggestion",
            "suggestion_id": "s1",
            "trigger_id": "t1",
            "ts": "2026-09-29T14:00:04-03:00",
            "text": "x",
            "frames_sent": ["f0001"],
            "backend": "b",
            "model": "m",
            "latency_ms": 10,
            "usage": {"input_tokens": 5, "cache_read_input_tokens": 7, "server_tool_use": 1},
            "new": "ignored",
        }
    )
    assert event == SuggestionLogged(
        "s1",
        "t1",
        datetime(2026, 9, 29, 14, 0, 4, tzinfo=BRT),
        "x",
        ["f0001"],
        "b",
        "m",
        10,
        Usage(input_tokens=5, cache_read_input_tokens=7),
    )
    action = parse_event(
        {"type": "action", "suggestion_id": "s1", "ts": "2026-09-29T14:00:04", "kind": "edit"}
    )
    assert action == ActionLogged("s1", datetime(2026, 9, 29, 14, 0, 4), ActionKind.EDIT)


def test_frames_ignore_extra_keys_and_count_bad_lines() -> None:
    frames, malformed = read_frames(FIXTURES / "full")
    assert [f.id for f in frames] == ["f0001", "f0002", "f0003", "f0004"]
    assert malformed == 0


def test_frames_bad_lines(tmp_path: Path) -> None:
    (tmp_path / "frames").mkdir()
    (tmp_path / "frames" / "index.jsonl").write_text('nope\n{"id":"f0001"}\n', encoding="utf-8")
    assert read_frames(tmp_path) == ([], 2)


def test_meeting_day_sources(tmp_path: Path) -> None:
    logged = [datetime(2026, 9, 29, 17, 0, tzinfo=UTC), datetime(2026, 9, 29, 16, 0, tzinfo=UTC)]
    assert meeting_day(tmp_path, logged) == logged[1]
    assert meeting_day(tmp_path, []).tzinfo is not None  # now, local

    toml = tmp_path / "meeting.toml"
    toml.write_text("started_at = 2026-09-29T14:30:00-03:00\n", encoding="utf-8")
    assert meeting_day(tmp_path, logged) == datetime(2026, 9, 29, 14, 30, tzinfo=BRT)
    toml.write_text('started_at = "2026-09-29T14:30:00-03:00"\n', encoding="utf-8")
    assert meeting_day(tmp_path, logged) == datetime(2026, 9, 29, 14, 30, tzinfo=BRT)
    for bad in ('started_at = "yesterday"\n', "started_at = 5\n", "not = [toml\n"):
        toml.write_text(bad, encoding="utf-8")
        assert meeting_day(tmp_path, logged) == logged[1]


# --- prices --------------------------------------------------------------------------


def test_price_table_file_and_fallback(tmp_path: Path) -> None:
    path = tmp_path / "prices.toml"
    path.write_text(
        '[models."a"]\ninput = 2\noutput = 10.5\n\n[models."*"]\ncache_read = 0.1\n',
        encoding="utf-8",
    )
    table = load_price_table(path)
    assert table.price_for("a") == ModelPrice(input=2.0, output=10.5)
    assert table.price_for("other") == ModelPrice(cache_read=0.1)
    assert ModelPrice(1, 2, 3, 4).cost(Usage(1_000_000, 1_000_000, 1_000_000, 1_000_000)) == 10
    assert parse_price_table("").price_for("a") is None


@pytest.mark.parametrize(
    "text",
    [
        "models = 3\n",
        '[models]\n"a" = 1\n',
        '[models."a"]\ninputs = 1\n',
        '[models."a"]\ninput = -1\n',
        '[models."a"]\ninput = "3"\n',
        '[models."a"]\ninput = true\n',
    ],
)
def test_price_table_rejects_bad_entries(text: str) -> None:
    with pytest.raises(ValueError, match="price table"):
        parse_price_table(text)


# --- missed triggers -----------------------------------------------------------------


def test_missed_triggers_with_aliases() -> None:
    missed = missed_triggers(FIXTURES / "full", ["Nick"])
    assert [(m.ts.strftime("%H:%M:%S"), m.alias) for m in missed] == [
        ("14:50:30", "Nick"),  # a mention with no trigger
        ("14:58:00", "Nick"),  # case-insensitive, speaker tag kept
    ]
    assert missed[1].line == "[14:58:00] OUTROS(2): NICK, e aí?"
    # "nickname" is not "Nick"; EU lines never count; lines near a trigger are not missed
    assert missed[0].to_dict()["line"] == "[14:50:30] OUTROS: o Nick falou isso ontem"


def test_missed_triggers_accent_insensitive_and_default_aliases() -> None:
    # Default aliases come from logged triggers: "Nick" and "Nik".
    assert len(missed_triggers(FIXTURES / "full")) == 2
    # Whisper spelled it "Nik" at 14:35:58, next to trigger t2: not missed.
    assert missed_triggers(FIXTURES / "full", ["Nik"]) == []
    assert missed_triggers(FIXTURES / "full", ["níck"])[0].alias == "níck"
    # A tight tolerance turns the near-trigger mentions into candidates too.
    assert len(missed_triggers(FIXTURES / "full", ["Nick"], tolerance_s=0.5)) == 7


def test_missed_triggers_without_aliases_or_transcript() -> None:
    assert missed_triggers(FIXTURES / "empty") == []
    assert missed_triggers(FIXTURES / "empty", [" "]) == []
    assert missed_triggers(FIXTURES / "errors_only") == []  # alias known, no transcript
