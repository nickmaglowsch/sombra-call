import pytest

from sombra.trigger.names import NameMatcher
from sombra.trigger.text import tokenize


def _aliases(text: str, *aliases: str) -> list[str]:
    return [h.alias for h in NameMatcher(aliases).find_all(tokenize(text))]


@pytest.mark.parametrize(
    "text",
    ["Nick, oi", "Nik, oi", "Nic, oi", "Nique, oi", "Nicky, oi", "e o relatório, nik?"],
)
def test_matches_whisper_misspellings(text: str) -> None:
    assert _aliases(text, "Nick") == ["Nick"]


def test_fuzzy_match_on_longer_names() -> None:
    hits = NameMatcher(["Nicolas"]).find_all(tokenize("Nicholas, qual o prazo?"))
    assert len(hits) == 1
    assert 0.5 < hits[0].similarity < 1.0


def test_fuzzy_match_needs_a_capitalised_word() -> None:
    # "dura" is one letter from "Duda" but is a common lowercase word.
    assert _aliases("essa parte é dura", "Duda") == []
    assert _aliases("Dura, pode falar?", "Duda") == ["Duda"]


def test_short_names_need_an_exact_key() -> None:
    assert _aliases("Nico, pode ver?", "Nick") == []
    assert _aliases("Você viu o Nike novo?", "Nick") == ["Nick"]  # same key, position decides


def test_fuzzy_needs_same_first_sound() -> None:
    assert _aliases("Diago, pode ver?", "Thiago") == []


def test_multi_word_alias_wins_over_its_prefix() -> None:
    hits = NameMatcher(["Ana", "Ana Paula"]).find_all(tokenize("Ana Paula, você pode revisar?"))
    assert [(h.alias, h.start, h.end) for h in hits] == [("Ana Paula", 0, 2)]


def test_multi_word_alias_cannot_span_punctuation() -> None:
    assert _aliases("Ana, Paula falou", "Ana Paula") == []


def test_finds_every_mention() -> None:
    assert _aliases("Nick, você viu o que o Nick fez?", "Nick") == ["Nick", "Nick"]


def test_needs_a_name() -> None:
    with pytest.raises(ValueError, match="at least one"):
        NameMatcher(["", "  ", "123"])
