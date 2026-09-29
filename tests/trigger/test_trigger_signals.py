import pytest

from sombra.trigger.names import NameMatcher
from sombra.trigger.signals import (
    VOCATIVE_END,
    VOCATIVE_FULL,
    VOCATIVE_MID,
    VOCATIVE_START,
    addresses_someone_else,
    is_deictic,
    is_phatic,
    name_position,
    request_strength,
)
from sombra.trigger.text import tokenize

MATCHER = NameMatcher(["Nick"])


def _position(text: str) -> tuple[float, bool]:
    tokens = tokenize(text)
    hits = MATCHER.find_all(tokens)
    assert hits, text
    pos = name_position(tokens, hits[0])
    return pos.vocative, pos.reference


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Nick, o que você acha?", (VOCATIVE_FULL, False)),
        ("Tá, e a migração, Nick?", (VOCATIVE_FULL, False)),
        ("E aí Nick, alguma novidade?", (VOCATIVE_FULL, False)),
        ("Ô Nick, você lembra?", (VOCATIVE_FULL, False)),
        ("Nick?", (VOCATIVE_FULL, False)),
        ("o deploy sai hoje Nick?", (VOCATIVE_END, False)),
        ("Nick você acha que dá", (VOCATIVE_START, False)),
        ("você acha Nick que dá", (VOCATIVE_MID, False)),
        ("Falei com o Nick ontem", (0.0, True)),
        ("Pede pro Nick subir", (0.0, True)),
        ("Nick falou que o deploy é quinta", (0.0, True)),
    ],
)
def test_name_position(text: str, expected: tuple[float, bool]) -> None:
    assert _position(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "o que você acha?",
        "pode compartilhar a tela",
        "você acha que precisa de mais uma sprint",
        "dá pra subir antes da demo",
        "sua vez",
        "what do you think about it?",
        "tudo certo com o deploy?",
    ],
)
def test_requests(text: str) -> None:
    assert request_strength(text) >= 0.4


@pytest.mark.parametrize(
    "text",
    [
        "falei com ele ontem",
        "bom trabalho na demo",
        "valeu, ajudou bastante",
        "você tem razão",
        "só pra você saber, o cliente ligou",
        "",
    ],
)
def test_non_requests(text: str) -> None:
    assert request_strength(text) < 0.4


def test_closing_lowers_request() -> None:
    assert request_strength("valeu, pode seguir") < request_strength("pode seguir")


@pytest.mark.parametrize(
    ("text", "phatic"),
    [
        ("Nick, tudo bem?", True),
        ("Bom dia, Nick! Tudo certo?", True),
        ("Oi Nick, tá me ouvindo?", True),
        ("Nick, tá aí, né?", True),
        ("Nick, e aí?", False),
        ("Nick, tudo certo com o deploy?", False),
        ("Nick, qual o prazo?", False),
    ],
)
def test_phatic(text: str, phatic: bool) -> None:
    tokens = tokenize(text)
    assert is_phatic(tokens, MATCHER.find_all(tokens)[0]) is phatic


def test_phatic_without_a_name() -> None:
    assert is_phatic(tokenize("tudo bem?"), None)
    assert not is_phatic(tokenize("?"), None)


@pytest.mark.parametrize(
    "text",
    [
        "o que você acha desse gráfico?",
        "tá vendo aqui esse botão?",
        "nessa tela que eu tô compartilhando",
        "essa planilha bate?",
        "esse slide tá claro?",
        "can you see this chart?",
    ],
)
def test_deictic(text: str) -> None:
    assert is_deictic(text)


@pytest.mark.parametrize(
    "text", ["o que ficou decidido ontem?", "qual o prazo do contrato?", "o que você acha disso?"]
)
def test_not_deictic(text: str) -> None:
    assert not is_deictic(text)


@pytest.mark.parametrize(
    ("text", "other"),
    [
        ("Maria, você pode me mandar?", True),
        ("Pessoal, o que vocês acham?", True),
        ("E aí Pedro, tudo certo?", True),
        ("Então, o que você acha?", False),
        ("o que você acha?", False),
        ("Maria", False),
        ("Sim, pode ser", False),
    ],
)
def test_addresses_someone_else(text: str, other: bool) -> None:
    assert addresses_someone_else(tokenize(text)) is other
