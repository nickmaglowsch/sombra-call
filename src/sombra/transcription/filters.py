"""Post-STT cleanup: drop Whisper hallucinations and build the vocabulary prompt (T2, T4).

Whisper invents text on noise and near-silence, mostly subtitle credits and sign-offs it
saw in training ("Legendas pela comunidade Amara.org", "Obrigado."), and sometimes loops
on one token. ``HallucinationFilter.clean`` removes non-speech tags, collapses loops and
returns ``None`` for text that is empty or only a known hallucination.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

# Whole-utterance outputs that are hallucinations when they are all Whisper heard.
# Compared after normalisation (lowercase, no accents, no punctuation).
DEFAULT_HALLUCINATIONS: tuple[str, ...] = (
    "legendas pela comunidade amara org",
    "legenda adriana zanotto",
    "legendado por",
    "transcricao e legendas",
    # Deliberate trade-off: a lone thanks is dropped even when really said (end of a call);
    # it is Whisper's most common output on near-silence and carries little meaning.
    # Could be kept for long or loud segments later.
    "obrigado",
    "obrigada",
    "muito obrigado",
    "muito obrigada",
    "obrigado por assistir",
    "obrigada por assistir",
    "obrigado por assistirem",
    "inscreva se no canal",
    "se inscreva no canal",
    "ate o proximo video",
    "ate a proxima",
    "musica",
    "subtitles by the amara org community",
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "you",
)

_TAG_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|♪+")
_WORD_RE = re.compile(r"\S+")


def normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse spaces: for comparisons only."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    no_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", no_marks).split())


def collapse_repeats(text: str, *, max_repeats: int = 3, max_ngram: int = 6) -> str:
    """Collapse an n-gram repeated more than ``max_repeats`` times in a row to one copy.

    "sim sim sim sim sim" -> "sim"; "é isso, é isso, é isso, é isso" -> "é isso,".
    Up to ``max_repeats`` repeats are kept, since people do say "não, não, não".
    """
    words = _WORD_RE.findall(text)
    keys = [normalize(w) for w in words]
    out: list[str] = []
    i = 0
    while i < len(words):
        collapsed = False
        for n in range(1, max_ngram + 1):
            gram = keys[i : i + n]
            if len(gram) < n or not any(gram):
                break
            reps = 1
            while keys[i + reps * n : i + (reps + 1) * n] == gram:
                reps += 1
            if reps > max_repeats:
                out.extend(words[i : i + n])
                i += reps * n
                collapsed = True
                break
        if not collapsed:
            out.append(words[i])
            i += 1
    return " ".join(out)


class HallucinationFilter:
    """Drops known hallucinations, loops, and echoes of the ``initial_prompt``."""

    def __init__(
        self,
        phrases: Iterable[str] = DEFAULT_HALLUCINATIONS,
        *,
        max_repeats: int = 3,
        prompt: str = "",
    ) -> None:
        self.max_repeats = max_repeats
        self._keys = frozenset(normalize(p) for p in phrases)
        if prompt:
            self._keys |= {normalize(prompt)}

    def clean(self, text: str) -> str | None:
        """Return the text to keep, or ``None`` to drop the utterance."""
        text = _TAG_RE.sub(" ", text)
        text = " ".join(text.split())
        if not text:
            return None
        text = collapse_repeats(text, max_repeats=self.max_repeats)
        key = normalize(text)
        if not key or not any(c.isalpha() for c in key):
            return None
        if key in self._keys:
            return None
        return text


def build_initial_prompt(vocabulary: Iterable[str], *, max_chars: int = 600) -> str:
    """Whisper ``initial_prompt`` from meeting vocabulary (names, acronyms, jargon) (T4).

    Whisper treats the prompt as preceding text, so a short PT-BR sentence listing the
    terms biases spelling without dictating content. Duplicates are dropped, order kept.
    """
    seen: set[str] = set()
    terms: list[str] = []
    for raw in vocabulary:
        term = " ".join(raw.split())
        if term and term.casefold() not in seen:
            seen.add(term.casefold())
            terms.append(term)
    if not terms:
        return ""
    prompt = "Reunião. Termos: "
    kept: list[str] = []
    for term in terms:
        if len(prompt) + len(", ".join([*kept, term])) + 1 > max_chars:
            break
        kept.append(term)
    return prompt + ", ".join(kept) + "." if kept else ""
