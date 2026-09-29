import pytest

from sombra.trigger.text import fold, phonetic_key, tokenize


def test_fold_strips_accents_and_case() -> None:
    assert fold("Você já viu o GRÁFICO?") == "voce ja viu o grafico?"


def test_tokenize_keeps_punctuation_and_raw_case() -> None:
    tokens = tokenize("Nick, você acha?")
    assert [t.raw for t in tokens] == ["Nick", ",", "você", "acha", "?"]
    assert [t.text for t in tokens] == ["nick", ",", "voce", "acha", "?"]
    assert [t.is_word for t in tokens] == [True, False, True, True, False]
    assert tokens[1].is_boundary
    assert not tokens[0].is_boundary


@pytest.mark.parametrize("spelling", ["Nick", "Nik", "Nic", "Nique", "Nicky", "Niki", "NICK"])
def test_whisper_spellings_of_nick_share_a_key(spelling: str) -> None:
    assert phonetic_key(spelling) == "nik"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Thiago", "Tiago"),
        ("Luiz", "Luís"),
        ("Rafael", "Rafaell"),
        ("Conceição", "Conceisão"),
        ("Nicolas", "Nicolás"),
    ],
)
def test_sound_alike_names_share_a_key(a: str, b: str) -> None:
    assert phonetic_key(a) == phonetic_key(b)


def test_phonetic_key_ignores_non_letters() -> None:
    assert phonetic_key("Nick's") == phonetic_key("Nicks")
    assert phonetic_key("123") == ""
