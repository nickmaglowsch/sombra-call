"""G2 and G4 signals on one utterance: vocative position, request structure, deictics.

All matching runs on accent/case-folded text (see :mod:`sombra.trigger.text`), so the
patterns below are written without accents.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sombra.trigger.names import NameHit
from sombra.trigger.text import Token, fold


def _wordset(words: str) -> frozenset[str]:
    return frozenset(words.split())


# --- vocative position (G2) ----------------------------------------------------------

# A word right before the name that makes it a reference, not a call: "o Nick", "pro Nick".
_REFERENCE_BEFORE = _wordset(
    "o a os as do da dos das no na nos nas pro pra pros pras pelo pela ao aos com de "
    "sem sobre para por um uma que the with to for about from ask tell"
)
# Words that may sit between the start of a clause and a vocative: "e aí Nick, ...".
_INTERJECTIONS = _wordset(
    "e ai oi ola ei hey hi opa entao ta ok okay beleza pois olha agora bom dia tarde noite "
    "fala so ne mas and ah eh hmm hein tipo cara bem enfim"
)
# A 3rd-person verb right after the name (no comma) makes the name the subject:
# "Nick falou que ...", "Nick vai apresentar?".
_SUBJECT_VERBS = _wordset(
    "disse falou fez ficou mandou comentou pediu vai foi esta estava tava tinha tem ja "
    "era sabe acha achou acabou subiu entrou saiu chegou pegou ia deve precisa quer queria "
    "conseguiu apresentou mostrou fechou tambem sempre nunca ainda so "
    "said says did will was is has had thinks"
)
_END_PUNCT = frozenset("?!.…")

VOCATIVE_FULL = 1.0  # "Nick, ..." / "..., Nick?" / "Nick?"
VOCATIVE_END = 0.8  # "... sai hoje Nick?"
VOCATIVE_START = 0.6  # "Nick o que você acha" (Whisper dropped the comma)
VOCATIVE_MID = 0.3  # "você acha Nick que dá"


@dataclass(frozen=True, slots=True)
class Position:
    vocative: float  # 0..1, how much the name position looks like someone calling the user
    reference: bool  # the name is talked about ("o Nick", "Nick falou que")
    implied_question: bool = False  # "Nick você já testou isso" (Whisper dropped the "?")


def _starts_clause(tokens: Sequence[Token], i: int) -> bool:
    """True if everything before ``i`` back to a boundary (or line start) is interjections."""
    j = i - 1
    while j >= 0:
        tok = tokens[j]
        if tok.is_boundary:
            return True
        if not tok.is_word:
            return False
        if tok.text not in _INTERJECTIONS and tok.raw not in ("ô", "Ô", "ó", "Ó"):
            return False
        j -= 1
    return True


def name_position(tokens: Sequence[Token], hit: NameHit) -> Position:
    prev = tokens[hit.start - 1] if hit.start > 0 else None
    nxt = tokens[hit.end] if hit.end < len(tokens) else None
    if (
        prev is not None
        and prev.is_word
        and prev.text in _REFERENCE_BEFORE
        and prev.raw not in ("ô", "Ô", "ó", "Ó")
    ):
        return Position(0.0, reference=True)
    before_ok = _starts_clause(tokens, hit.start)
    after_ok = nxt is None or nxt.is_boundary
    if nxt is not None and nxt.is_word:
        if nxt.text in _SUBJECT_VERBS and before_ok:
            return Position(0.0, reference=True)
        if (
            nxt.raw[:1].isupper()
            and nxt.text not in _SECOND_PERSON_WORDS
            and not _ADDRESSED_OPENING.match(nxt.text)
        ):
            return Position(0.0, reference=True)  # a namesake's full name: "Nicolas Cage ..."
    if before_ok and after_ok:
        return Position(VOCATIVE_FULL, reference=False)
    if nxt is None or nxt.text in _END_PUNCT:
        return Position(VOCATIVE_END, reference=False)
    if before_ok:
        return _start_position(tokens[hit.end :])
    return Position(VOCATIVE_MID, reference=False)


def _start_position(rest: Sequence[Token]) -> Position:
    """Name opens the line with no comma: a call only if what follows talks *to* the user.

    "Nick você já testou isso" and "Nick o que você acha" are calls with a dropped
    comma; "Nick tá de férias, né?" is a statement about Nick to the group, where the
    only question is the tag at the end.
    """
    text = " ".join(t.raw for t in rest)
    folded = fold(text)
    if rest[0].text in _SECOND_PERSON_WORDS:
        # "nick você tem razão" is praise, not a question with a dropped "?"
        implied = _EVALUATIVE.search(folded) is None
        return Position(VOCATIVE_START, reference=False, implied_question=implied)
    if _ADDRESSED_OPENING.match(folded):
        return Position(VOCATIVE_START, reference=False)
    if (m := _TAG_QUESTION.search(folded)) and request_strength(folded[: m.start()]) < 0.4:
        return Position(0.0, reference=True)
    return Position(VOCATIVE_START, reference=False)


# Words that open a sentence with a comma without addressing anyone ("Então, ...").
_NEUTRAL_OPENERS = _INTERJECTIONS | _wordset(
    "sim nao certo isso exato perfeito legal show claro beleza tranquilo pronto enfim "
    "ali depois antes alias bom ok well yeah yes no right"
)


def addresses_someone_else(tokens: Sequence[Token]) -> bool:
    """The line opens by calling another person or the group: "Maria, ...", "Pessoal, ..."."""
    i = 0
    while i < len(tokens) and tokens[i].is_word and tokens[i].text in _INTERJECTIONS:
        i += 1
    if i + 1 >= len(tokens):
        return False
    first, second = tokens[i], tokens[i + 1]
    return (
        first.is_word
        and first.raw[:1].isupper()
        and second.text == ","
        and first.text not in _NEUTRAL_OPENERS
    )


# --- request structure (G2) ----------------------------------------------------------


def _words(*phrases: str) -> re.Pattern[str]:
    alternatives = "|".join(sorted((re.escape(p) for p in phrases), key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")


def _opening(*patterns: re.Pattern[str]) -> re.Pattern[str]:
    return re.compile("^(?:" + "|".join(p.pattern for p in patterns) + ")")


_INTERROGATIVE = _words(
    "o que", "oque", "que que", "qual", "quais", "quando", "quem", "por que", "porque",
    "pq", "como", "onde", "cade", "quanto", "quantos", "quantas", "sera que", "what",
    "how", "when", "why", "where", "which", "who",
)  # fmt: skip
_OPINION = _words(
    "voce acha", "vc acha", "ce acha", "tu acha", "acha que", "o que acha", "que acha",
    "concorda", "opiniao", "topa", "what do you think", "any thoughts", "do you agree",
    "thoughts on",
)  # fmt: skip
_REQUEST = _words(
    "fala", "falar", "conta", "explica", "explicar", "me diz", "me diga", "diz ai", "diga",
    "pode", "poderia", "podia", "consegue", "conseguiria", "da pra", "tem como",
    "compartilha", "mostra", "comenta", "confirma", "manda", "passa", "apresenta",
    "detalha", "resume", "can you", "could you", "would you", "tell us", "walk us",
)  # fmt: skip
_WEAK_REQUEST = _words(
    "sabe", "lembra", "viu", "tem", "ta", "esta", "vai", "quer", "precisa", "chegou",
    "do you", "did you", "are you", "have you", "is it",
)  # fmt: skip
_ADDRESSED_OPENING = _opening(_INTERROGATIVE, _OPINION, _REQUEST)

# Standup turn handoffs: "Nick, sua vez", "agora contigo, Nick".
_HANDOFF = _words(
    "sua vez", "tua vez", "contigo", "com voce", "com vc", "e voce", "e vc", "e ce",
    "your turn", "over to you",
)  # fmt: skip
_SECOND_PERSON = _words(
    "voce", "voces", "vc", "vcs", "ce", "tu", "te", "contigo", "you", "your",
)  # fmt: skip
_SECOND_PERSON_WORDS = _wordset("voce vc ce tu you")
_EVALUATIVE = _words(
    "tem razao", "manda bem", "manda muito bem", "mandou bem", "mandou muito bem", "arrasou",
    "arrasa", "e o cara", "e demais", "salvou", "e fera",
)  # fmt: skip
# Tag questions turn a statement into a "question" without asking the listener anything.
_TAG_QUESTION = re.compile(
    r"(?:^|[\s,])(?:ne|ne nao|nao e|nao foi|nao \w+|certo|ok|right)"
    r"\s*\?\s*$"
)

_CLOSING = _words(
    "valeu", "obrigado", "obrigada", "brigado", "brigada", "tchau", "ate mais",
    "ate amanha", "thanks", "thank you", "bom trabalho", "parabens",
)  # fmt: skip
# A greeting or line check with nothing else in it needs no answer from an agent.
_PHATIC = re.compile(
    r"^(?:(?:bom dia|boa tarde|boa noite|oi|ola|e ai|opa|fala|hey|hi|hello|"
    r"tudo (?:bem|bom|certo|joia|tranquilo|ok)|beleza|blz|suave|como (?:vai|voce ta|vc ta|ce ta)|"
    r"(?:voce |vc |ce )?(?:ta|esta) (?:ai|me ouvindo|ouvindo|com a gente|na call)|"
    r"(?:voce |vc |ce )?(?:me )?(?:ouve|escuta)|ne|hein|how are you|can you hear me)(?: |$))+$"
)

_BARE_OPENERS = _wordset("e ai oi opa fala hey hi ne hein")

W_QUESTION_MARK = 0.6
W_INTERROGATIVE = 0.35
W_OPINION = 0.5
W_REQUEST = 0.4
W_WEAK_REQUEST = 0.2
W_HANDOFF = 0.6
W_SECOND_PERSON = 0.2
W_CLOSING = -0.4


def request_strength(text: str) -> float:
    """0..1: how much ``text`` asks something of the listener (question, request, opinion)."""
    folded = fold(text)
    s = 0.0
    asks = "?" in folded
    if asks:
        s += W_QUESTION_MARK
    if _INTERROGATIVE.search(folded):
        s += W_INTERROGATIVE
        asks = True
    if _OPINION.search(folded):
        s += W_OPINION
    if _HANDOFF.search(folded):
        s += W_HANDOFF
    if _REQUEST.search(folded):
        s += W_REQUEST
    elif asks and _WEAK_REQUEST.search(folded):
        s += W_WEAK_REQUEST
    # "você" and weak verbs only add up inside a question: "Nick, você tem razão" asks nothing.
    if asks and _SECOND_PERSON.search(folded):
        s += W_SECOND_PERSON
    if _CLOSING.search(folded) and "?" not in folded:
        s += W_CLOSING
    return max(0.0, min(1.0, s))


def is_phatic(tokens: Sequence[Token], hit: NameHit | None) -> bool:
    """True for a bare greeting or line check ("Nick, tudo bem?", "tá me ouvindo, Nick?")."""
    words = [
        t.text
        for i, t in enumerate(tokens)
        if t.is_word and (hit is None or not hit.start <= i < hit.end)
    ]
    if not words or set(words) <= _BARE_OPENERS:
        return False  # "Nick, e aí?" hands the turn over; it is not a greeting
    return bool(_PHATIC.match(" ".join(words) + " "))


# --- deictics (G4) -------------------------------------------------------------------

_DEMONSTRATIVE = sorted(
    _wordset(
        "esse essa este esta nesse nessa neste nesta desse dessa deste desta aquele aquela "
        "naquele naquela daquele daquela"
    )
)
_SCREEN_NOUNS = sorted(
    _wordset(
        "grafico graficos tela planilha slide slides tabela imagem dashboard numero numeros "
        "coluna linha diagrama print card botao layout figura mapa board quadro documento "
        "pagina campo curva barra pico valor mockup prototipo ticket"
    )
)
_DEICTIC = re.compile(
    r"(?<!\w)(?:"
    rf"(?:{'|'.join(_DEMONSTRATIVE)}) (?:\w+ )?(?:{'|'.join(_SCREEN_NOUNS)})"
    r"|aqui|ali em cima|ali embaixo|(?:ta|tao|esta|estao|ce ta|voce ta|vc ta) vendo"
    r"|olha isso|olha so|olha esse|olha essa|na tela|nessa tela|compartilhando|mostrando"
    r"|this (?:chart|slide|screen|graph|table|dashboard|number)|on (?:the )?screen"
    r"|right here|over here|you see this"
    r")(?!\w)"
)


def is_deictic(text: str) -> bool:
    """G4: the question points at something on screen ("esse gráfico", "tá vendo aqui")."""
    return _DEICTIC.search(fold(text)) is not None
