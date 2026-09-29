from datetime import UTC, datetime, timedelta

import pytest

from sombra.contracts import (
    Channel,
    FrameMarker,
    SpeechLine,
    TimelineEntry,
    TriggerDetector,
    TriggerEvent,
)
from sombra.trigger import NameTriggerDetector, TriggerSettings

T0 = datetime(2026, 9, 29, 14, 0, 0, tzinfo=UTC)


def at(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


def others(s: float, text: str) -> SpeechLine:
    return SpeechLine(at(s), Channel.OTHERS, text)


def me(s: float, text: str) -> SpeechLine:
    return SpeechLine(at(s), Channel.ME, text)


def frame(s: float, n: int, title: str = "Zoom") -> FrameMarker:
    return FrameMarker(at(s), f"f{n:04d}", title)


def feed_all(detector: TriggerDetector, entries: list[TimelineEntry]) -> list[TriggerEvent]:
    return [e for entry in entries if (e := detector.feed(entry)) is not None]


def make(**kwargs: float) -> NameTriggerDetector:
    return NameTriggerDetector("Nick", ["Nicolas"], TriggerSettings(**kwargs))


def test_implements_the_port() -> None:
    detector: TriggerDetector = make()
    assert detector.feed(others(0, "bom dia")) is None


def test_fires_on_a_direct_question() -> None:
    event = make().feed(others(0, "Nick, o que você acha de fechar na sexta?"))
    assert event is not None
    assert event.question == "Nick, o que você acha de fechar na sexta?"
    assert event.matched_alias == "Nick"
    assert event.ts == at(0)
    assert 0.7 <= event.score <= 1.0
    assert not event.needs_screen
    assert event.candidate_frames == ()


def test_matched_alias_is_the_configured_one() -> None:
    event = make().feed(others(0, "Nicholas, qual é o prazo?"))
    assert event is not None
    assert event.matched_alias == "Nicolas"
    assert event.score < 1.0


def test_eu_lines_never_trigger() -> None:
    d = make()
    assert d.feed(me(0, "Nick, o que você acha disso?")) is None
    assert d.feed(me(1, "Nick,")) is None
    assert d.feed(others(2, "o que você acha?")) is None  # an EU name line is no split either


@pytest.mark.parametrize(
    "text",
    [
        "Falei com o Nick ontem.",
        "O Nick tá cuidando disso.",
        "Valeu, Nick.",
        "Nick falou que o deploy é quinta.",
        "Nick",
        "Nick, bom trabalho.",
    ],
)
def test_name_only_never_triggers(text: str) -> None:
    assert make().feed(others(0, text)) is None


def test_question_without_the_name_does_not_trigger() -> None:
    assert make().feed(others(0, "O que vocês acham?")) is None


def test_split_name_then_question() -> None:
    d = make()
    assert d.feed(others(0, "Nick...")) is None
    event = d.feed(others(3, "o que você acha dessa abordagem?"))
    assert event is not None
    assert event.question == "Nick... o que você acha dessa abordagem?"
    assert event.ts == at(3)


def test_split_question_then_name() -> None:
    d = make()
    assert d.feed(others(0, "E isso já foi pro cliente?")) is None
    event = d.feed(others(2, "Nick?"))
    assert event is not None
    assert event.question == "E isso já foi pro cliente? Nick?"


def test_split_survives_frame_markers_and_eu_lines() -> None:
    d = make()
    events = feed_all(d, [others(0, "Nick,"), frame(1, 1), me(2, "oi"), others(3, "tá vendo?")])
    assert len(events) == 1


def test_split_expires() -> None:
    events = feed_all(make(), [others(0, "Nick..."), others(6, "o que você acha?")])
    assert events == []


def test_split_split_s_is_configurable() -> None:
    events = feed_all(make(split_s=10), [others(0, "Nick..."), others(6, "o que você acha?")])
    assert len(events) == 1


def test_split_ignores_a_question_to_someone_else() -> None:
    events = feed_all(make(), [others(0, "Nick"), others(2, "Maria, você pode me mandar?")])
    assert events == []


def test_split_question_needs_a_bare_vocative() -> None:
    d = make()
    events = feed_all(d, [others(0, "Quem cuida disso?"), others(2, "Nick fez o deploy ontem.")])
    assert events == []


def test_window_keeps_the_last_60_seconds() -> None:
    d = make()
    old = others(0, "Vamos começar.")
    kept = [frame(10, 1), me(30, "acho que dá"), others(50, "Hoje teve três incidentes.")]
    feed_all(d, [old, *kept])
    event = d.feed(others(65, "Nick, você acha que dá pra investigar hoje?"))
    assert event is not None
    assert list(event.window) == [*kept, others(65, "Nick, você acha que dá pra investigar hoje?")]


def test_window_s_is_configurable() -> None:
    d = make(window_s=10)
    feed_all(d, [others(0, "a"), others(8, "b")])
    event = d.feed(others(15, "Nick, e o prazo?"))
    assert event is not None
    assert [e.text for e in event.window if isinstance(e, SpeechLine)] == ["b", "Nick, e o prazo?"]


def test_deictic_question_picks_current_frame_first() -> None:
    d = make()
    feed_all(d, [frame(0, 1), frame(5, 2), frame(10, 3), frame(15, 4)])
    event = d.feed(others(17, "Nick, o que você acha desse gráfico?"))
    assert event is not None
    assert event.needs_screen
    assert event.candidate_frames == ("f0004", "f0003", "f0002")


def test_frames_are_distinct() -> None:
    d = make()
    feed_all(d, [frame(0, 1), frame(5, 2), frame(6, 2), frame(10, 1)])
    event = d.feed(others(12, "Nick, tá vendo aqui?"))
    assert event is not None
    assert event.candidate_frames == ("f0001", "f0002")


def test_deictic_without_frames_still_needs_screen() -> None:
    event = make().feed(others(0, "Nick, nessa tela dá pra ver o erro?"))
    assert event is not None
    assert event.needs_screen
    assert event.candidate_frames == ()


def test_no_frames_when_the_screen_does_not_matter() -> None:
    d = make()
    d.feed(frame(0, 1))
    event = d.feed(others(2, "Nick, qual é o prazo do contrato?"))
    assert event is not None
    assert not event.needs_screen
    assert event.candidate_frames == ()


def test_frames_older_than_the_window_still_count() -> None:
    d = make()
    feed_all(d, [frame(0, 1), others(100, "bla")])
    event = d.feed(others(120, "Nick, esse slide tá bom?"))
    assert event is not None
    assert event.candidate_frames == ("f0001",)


def test_cooldown_blocks_a_second_trigger() -> None:
    d = make()
    assert d.feed(others(0, "Nick, você pode revisar o PR?")) is not None
    assert d.feed(others(10, "Nick, e o prazo do contrato?")) is None
    assert d.feed(others(31, "Nick, e o prazo do contrato?")) is not None


def test_dedupe_blocks_a_repeated_question_after_the_cooldown() -> None:
    d = make()
    assert d.feed(others(0, "Nick, você pode revisar o PR?")) is not None
    assert d.feed(others(40, "Nick, você pode revisar o PR??")) is None
    assert d.feed(others(200, "Nick, você pode revisar o PR?")) is not None


def test_cooldown_zero_allows_back_to_back_different_questions() -> None:
    d = make(cooldown_s=0)
    assert d.feed(others(0, "Nick, você pode revisar o PR?")) is not None
    assert d.feed(others(1, "Nick, e o prazo do contrato?")) is not None


def test_threshold_is_a_parameter() -> None:
    assert make(threshold=1.01).feed(others(0, "Nick, o que você acha?")) is None
    # weak structure: name at the start without a comma, request verb only
    assert make().feed(others(0, "Nick pode compartilhar a tela")) is not None
    assert make(threshold=0.9).feed(others(0, "Nick pode compartilhar a tela")) is None


def test_trigger_ids_are_unique() -> None:
    d = make(cooldown_s=0, dedupe_s=0)
    a = d.feed(others(0, "Nick, você pode revisar o PR?"))
    b = d.feed(others(1, "Nick, e o prazo do contrato?"))
    assert a is not None and b is not None
    assert a.id != b.id


def test_prefers_the_vocative_mention() -> None:
    event = make().feed(others(0, "Você viu o que o Nick mandou, Nick?"))
    assert event is not None


def test_phatic_greeting_does_not_trigger() -> None:
    assert make().feed(others(0, "Nick, tudo bem?")) is None


@pytest.mark.parametrize(
    "text",
    ["Nick tá de férias, né?", "Nick fechou com o cliente, não foi?", "Nicolas Cage fez isso, né?"],
)
def test_statements_about_the_user_do_not_trigger(text: str) -> None:
    assert make().feed(others(0, text)) is None


def test_dropped_question_mark_after_voce_still_triggers() -> None:
    assert make().feed(others(0, "nick você já testou isso em produção")) is not None


@pytest.mark.parametrize(
    "text",
    [
        "E aí Nick tá tudo certo com o deploy?",
        "Nick tá conseguindo ver minha tela?",
        "Nick Pode compartilhar a tela?",
    ],
)
def test_dropped_comma_questions_still_trigger(text: str) -> None:
    assert make().feed(others(0, text)) is not None


def test_praise_without_a_comma_is_not_an_implied_question() -> None:
    assert make().feed(others(0, "nick você tem razão")) is None
