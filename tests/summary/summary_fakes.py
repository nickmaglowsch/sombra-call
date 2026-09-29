"""Fakes shared by the summary tests (imported as a plain module; no conftest)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sombra.contracts import Channel, SpeechLine, Usage

DAY = datetime(2026, 9, 29, 14, 0, 0)


@dataclass
class Call:
    system: str
    user: str
    max_tokens: int


@dataclass
class FakeModel:
    """``TextModel`` fake: ``reply(system, user)`` builds the answer; calls are recorded."""

    reply: Callable[[str, str], str]
    name: str = "fake-model"
    calls: list[Call] = field(default_factory=list)

    def complete(self, system: str, user: str, max_tokens: int) -> tuple[str, Usage]:
        self.calls.append(Call(system, user, max_tokens))
        return self.reply(system, user), Usage(input_tokens=len(user), output_tokens=10)


def transcript_times(user: str) -> list[str]:
    return re.findall(r"^\[(\d{2}:\d{2}:\d{2})\]", user, flags=re.M)


def speech(
    start: datetime, seconds: int, text: str, channel: Channel = Channel.OTHERS
) -> SpeechLine:
    return SpeechLine(ts=start + timedelta(seconds=seconds), channel=channel, text=text)


def synthetic_lines(hours: float, every_s: int = 4, start: datetime = DAY) -> list[str]:
    """A long synthetic PT-BR transcript, one line every ``every_s`` seconds."""
    phrases = [
        "a gente precisa fechar o escopo do painel de relatórios ainda essa semana",
        "Nick, você consegue revisar o PR do checkout antes do almoço?",
        "acho que dá pra fechar na sexta se o time de dados ajudar",
        "o deploy de terça deixou o cálculo de desconto mais lento",
    ]
    out = []
    n = int(hours * 3600 / every_s)
    for i in range(n):
        ts = start + timedelta(seconds=i * every_s)
        who = "EU" if i % 5 == 0 else "OUTROS"
        out.append(f"[{ts:%H:%M:%S}] {who}: {phrases[i % len(phrases)]} ({i})")
    return out
