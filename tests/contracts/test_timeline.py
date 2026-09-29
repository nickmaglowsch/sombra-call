from datetime import datetime, timedelta, timezone

import pytest

from sombra.contracts import Channel, FrameMarker, SpeechLine, format_frame_id, parse_line

BRT = timezone(timedelta(hours=-3))
DAY = datetime(2026, 9, 29, tzinfo=BRT)
T = datetime(2026, 9, 29, 14, 32, 10, tzinfo=BRT)


def test_speech_line_format() -> None:
    line = SpeechLine(ts=T, channel=Channel.OTHERS, text="Nick,  o que\nvocê acha?").to_line()
    assert line == "[14:32:10] OUTROS: Nick, o que você acha?"


def test_speech_line_with_speaker() -> None:
    line = SpeechLine(ts=T, channel=Channel.OTHERS, text="ok", speaker="2").to_line()
    assert line == "[14:32:10] OUTROS(2): ok"


def test_frame_marker_format_matches_prd() -> None:
    line = FrameMarker(ts=T, frame_id="f0123", window_title="Zoom - Roadmap Q4").to_line()
    assert line == '[14:32:10] TELA f0123 "Zoom - Roadmap Q4"'


def test_frame_marker_sanitises_quotes() -> None:
    line = FrameMarker(ts=T, frame_id="f0001", window_title='say "hi"').to_line()
    assert line == "[14:32:10] TELA f0001 \"say 'hi'\""


@pytest.mark.parametrize(
    "entry",
    [
        SpeechLine(ts=T, channel=Channel.ME, text="fechamos na sexta"),
        SpeechLine(ts=T, channel=Channel.OTHERS, text="a: b: c", speaker="3"),
        FrameMarker(ts=T, frame_id="f12345", window_title="Figma — Dashboard"),
    ],
)
def test_round_trip(entry: SpeechLine | FrameMarker) -> None:
    assert parse_line(entry.to_line() + "\n", day=DAY) == entry


def test_parse_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="not a timeline line"):
        parse_line("hello", day=DAY)


def test_frame_ids() -> None:
    assert format_frame_id(7) == "f0007"
    assert format_frame_id(12345) == "f12345"
    with pytest.raises(ValueError):
        format_frame_id(-1)
    with pytest.raises(ValueError):
        FrameMarker(ts=T, frame_id="0007", window_title="x")
