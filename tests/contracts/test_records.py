import json
from datetime import UTC, datetime

import pytest

from sombra.contracts import (
    ActionKind,
    ActionLogged,
    FrameRecord,
    SuggestionLogged,
    Usage,
    log_event_type,
    to_json_line,
)

T = datetime(2026, 9, 29, 17, 32, 10, tzinfo=UTC)


def test_frame_record_has_no_type_key() -> None:
    rec = FrameRecord(
        id="f0001",
        ts=T,
        path="frames/f0001.jpg",
        width=1280,
        height=720,
        app="zoom.us",
        window_title="Zoom - Roadmap Q4",
        diff_score=12.0,
        phash="ff00ff00ff00ff00",
    )
    data = json.loads(to_json_line(rec))
    assert "type" not in data
    assert data["ts"] == "2026-09-29T17:32:10+00:00"


def test_log_event_is_single_line_with_type() -> None:
    ev = SuggestionLogged(
        suggestion_id="s1",
        trigger_id="t1",
        ts=T,
        text="Sim,\nfecha na sexta.",
        frames_sent=["f0001"],
        backend="claude-agent-sdk",
        model="test-model",
        latency_ms=4200,
        usage=Usage(input_tokens=10, cache_read_input_tokens=9000),
    )
    line = to_json_line(ev)
    assert "\n" not in line
    data = json.loads(line)
    assert data["type"] == "suggestion"
    assert data["usage"]["cache_read_input_tokens"] == 9000
    assert log_event_type(line) == "suggestion"


def test_enum_serialises_to_value() -> None:
    line = to_json_line(ActionLogged(suggestion_id="s1", ts=T, kind=ActionKind.NOT_FOR_ME))
    assert json.loads(line)["kind"] == "not_for_me"


def test_log_event_type_requires_type() -> None:
    with pytest.raises(ValueError):
        log_event_type('{"x": 1}')
