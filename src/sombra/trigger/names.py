"""G1: fuzzy match of the user's name and aliases inside one utterance.

A name matches when its phonetic key (see :func:`sombra.trigger.text.phonetic_key`)
equals the word's key, or is within a small edit distance of it. Fuzzy (non-exact)
matches also need the word to be capitalised, as Whisper writes proper nouns, so that
common lowercase words one letter away from a short name ("dura" vs "Duda") never match.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rapidfuzz.distance import Levenshtein

from sombra.trigger.text import Token, phonetic_key


@dataclass(frozen=True, slots=True)
class NameHit:
    alias: str  # the configured alias that matched, as configured
    start: int  # index of the first matched token in the token list
    end: int  # index one past the last matched token
    similarity: float  # 1.0 for an exact phonetic match, lower for fuzzy ones


def _max_distance(key: str) -> int:
    if len(key) <= 3:
        return 0
    if len(key) <= 6:
        return 1
    return 2


def _word_similarity(alias_key: str, key: str, raw: str) -> float:
    if not key:
        return 0.0
    if key == alias_key:
        return 1.0
    allowed = _max_distance(alias_key)
    if allowed == 0 or key[0] != alias_key[0] or not raw[:1].isupper():
        return 0.0
    dist = Levenshtein.distance(key, alias_key, score_cutoff=allowed)
    if dist > allowed:
        return 0.0
    return 1.0 - 0.15 * dist


class NameMatcher:
    """Finds the configured aliases (one or more words each) in a tokenised utterance."""

    def __init__(self, aliases: Sequence[str]) -> None:
        self._aliases: list[tuple[str, tuple[str, ...]]] = []
        for alias in aliases:
            keys = tuple(k for k in (phonetic_key(w) for w in alias.split()) if k)
            if keys:
                self._aliases.append((alias, keys))
        if not self._aliases:
            raise ValueError("need at least one non-empty name or alias")

    def find_all(self, tokens: Sequence[Token]) -> list[NameHit]:
        """Every non-overlapping hit, left to right; the longest alias wins an overlap."""
        words = [i for i, t in enumerate(tokens) if t.is_word]
        keys = {i: phonetic_key(tokens[i].text) for i in words}
        hits: list[NameHit] = []
        for alias, alias_keys in self._aliases:
            n = len(alias_keys)
            for w in range(len(words) - n + 1):
                idx = words[w : w + n]
                if idx[-1] - idx[0] != n - 1:  # punctuation inside a multi-word alias
                    continue
                sim = min(
                    _word_similarity(k, keys[i], tokens[i].raw)
                    for k, i in zip(alias_keys, idx, strict=True)
                )
                if sim > 0.0:
                    hits.append(NameHit(alias, idx[0], idx[-1] + 1, sim))
        hits.sort(key=lambda h: (h.start, -(h.end - h.start), -h.similarity))
        kept: list[NameHit] = []
        for hit in hits:
            if not kept or hit.start >= kept[-1].end:
                kept.append(hit)
        return kept
