"""Hallucination filter and vocabulary prompt, with PT-BR Whisper outputs."""

from __future__ import annotations

import pytest

from sombra.transcription.filters import (
    HallucinationFilter,
    build_initial_prompt,
    collapse_repeats,
    normalize,
)

F = HallucinationFilter()


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "Legendas pela comunidade Amara.org",
        " legendas pela comunidade amara.org ",
        "Obrigado.",
        "Obrigada!",
        "Muito obrigado.",
        "Obrigado por assistir!",
        "Inscreva-se no canal!",
        "[Música]",
        "(risos)",
        "[BLANK_AUDIO]",
        "♪♪",
        "...",
        "- -",
        "Thank you.",
    ],
)
def test_drops_hallucinations_and_non_speech(raw: str) -> None:
    assert F.clean(raw) is None


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        ("acho que dá pra fechar na sexta", "acho que dá pra fechar na sexta"),
        ("  Nick,   o que você acha desse gráfico?  ", "Nick, o que você acha desse gráfico?"),
        ("Obrigado, Nick, era isso.", "Obrigado, Nick, era isso."),  # thanks inside speech stays
        ("[Música] vamos começar", "vamos começar"),
        ("não, não, não, espera", "não, não, não, espera"),  # 3 repeats are human
        ("sim sim sim sim sim sim sim", "sim"),
        ("é isso, é isso, é isso, é isso, é isso. Fechado", "é isso, Fechado"),
    ],
)
def test_keeps_real_speech(raw: str, kept: str) -> None:
    assert F.clean(raw) == kept


def test_loop_of_a_hallucination_is_still_dropped() -> None:
    assert F.clean("Obrigado. Obrigado. Obrigado. Obrigado. Obrigado.") is None


def test_custom_phrases_and_prompt_echo() -> None:
    f = HallucinationFilter(("fim",), prompt="Reunião. Termos: Laya, OKR.")
    assert f.clean("Fim.") is None
    assert f.clean("reunião termos laya okr") is None
    assert f.clean("Obrigado.") == "Obrigado."  # not in this custom list


def test_collapse_repeats_limits() -> None:
    assert collapse_repeats("a b a b a b a b c") == "a b c"
    assert collapse_repeats("a a a") == "a a a"
    assert collapse_repeats("um dois três", max_repeats=1) == "um dois três"
    assert collapse_repeats("") == ""


def test_normalize_strips_accents_and_punctuation() -> None:
    assert normalize("  Reunião, AMANHÃ às 10h!  ") == "reuniao amanha as 10h"


def test_initial_prompt_from_vocabulary() -> None:
    prompt = build_initial_prompt(["Nick", "Laya", " OKR ", "nick", "", "Roadmap  Q4"])
    assert prompt == "Reunião. Termos: Nick, Laya, OKR, Roadmap Q4."


def test_initial_prompt_empty_and_bounded() -> None:
    assert build_initial_prompt([]) == ""
    long = build_initial_prompt([f"Termo{i}" for i in range(500)], max_chars=120)
    assert len(long) <= 120 and long.endswith(".")
    assert build_initial_prompt(["x" * 200], max_chars=50) == ""
