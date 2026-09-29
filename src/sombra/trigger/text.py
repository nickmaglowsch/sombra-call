"""Text normalisation for PT-BR speech: accent/case folding, tokens and phonetic keys.

Everything here is pure and cheap; the detector runs it on every ``OUTROS`` line.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"\w+|[^\w\s]")
_PUNCT_BOUNDARY = frozenset(",.;:!?…-—–()\"'")


def fold(text: str) -> str:
    """Lowercase and strip accents: ``"Você já viu o gráfico?"`` -> ``"voce ja viu o grafico?"``."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


@dataclass(frozen=True, slots=True)
class Token:
    raw: str  # as written (keeps Whisper's capitalisation)
    text: str  # folded
    is_word: bool

    @property
    def is_boundary(self) -> bool:
        """Punctuation that separates a vocative from the rest of the sentence."""
        return not self.is_word and self.text in _PUNCT_BOUNDARY


def tokenize(text: str) -> list[Token]:
    """Split text into words and single punctuation marks, keeping their order."""
    return [Token(t, fold(t), t[0].isalnum() or t[0] == "_") for t in _TOKEN_RE.findall(text)]


# Ordered rewrite rules. They make the key tolerant to how Whisper spells a name it
# does not know: "Nick", "Nik", "Nic", "Nique" and "Nicky" all become "nik".
_PHONETIC_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p), r)
    for p, r in (
        (r"ph", "f"),
        (r"th", "t"),
        (r"ck", "k"),
        (r"qu", "k"),
        (r"q", "k"),
        (r"ch", "x"),
        (r"sh", "x"),
        (r"lh", "li"),
        (r"nh", "ni"),
        (r"c(?=[eiy])", "s"),
        (r"c", "k"),
        (r"ss", "s"),
        (r"z", "s"),
        (r"y", "i"),
        (r"w", "u"),
        (r"^h", ""),
        (r"(.)\1+", r"\1"),
        (r"(?<=[^aeiou])[ei]$", ""),  # "Nique"/"Nicky" -> nik, "Jorge" -> jorg
    )
)


def phonetic_key(word: str) -> str:
    """A rough PT/EN sound-alike key for one word (letters only)."""
    s = fold(word.casefold().replace("ç", "s"))
    s = "".join(c for c in s if c.isalpha())
    for pattern, repl in _PHONETIC_RULES:
        s = pattern.sub(repl, s)
    return s
