"""End-of-meeting minutes (M3, L0): summary, decisions, action items, open questions.

The model answers in JSON; we parse it, check every action item's ``[HH:MM:SS]``
against the transcript (hallucinated timestamps are dropped and logged), render PT-BR
markdown and write it to ``summary.md`` under ``## Ata``. Transcripts larger than the
model context are summarised with a map-reduce pass.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sombra.contracts import Usage
from sombra.summary import prompts
from sombra.summary.model import DEFAULT_CONTEXT_TOKENS, TextModel, add_usage
from sombra.summary.summary_file import write_minutes_section
from sombra.summary.transcript import (
    TranscriptLine,
    chunk_lines,
    estimate_tokens,
    read_transcript,
    render,
)

log = logging.getLogger(__name__)

TRANSCRIPT_FILE = "transcript.md"
SUMMARY_FILE = "summary.md"
DEFAULT_MINUTES_MAX_TOKENS = 4_096
_HMS_RE = re.compile(r"^\[?(\d{1,2}):(\d{2}):(\d{2})\]?$")


class MinutesParseError(ValueError):
    """The model's answer is not the expected JSON."""


@dataclass(frozen=True, slots=True)
class ActionItem:
    description: str
    owner: str | None
    due: str | None
    ref: str  # HH:MM:SS; validated against the transcript before rendering


@dataclass(frozen=True, slots=True)
class Minutes:
    summary: str
    decisions: list[str] = field(default_factory=list)
    actions: list[ActionItem] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "resumo": self.summary,
                "decisoes": self.decisions,
                "acoes": [
                    {
                        "descricao": a.description,
                        "responsavel": a.owner,
                        "prazo": a.due,
                        "ref": a.ref,
                    }
                    for a in self.actions
                ],
                "perguntas_abertas": self.open_questions,
            },
            ensure_ascii=False,
        )


@dataclass(frozen=True, slots=True)
class MinutesResult:
    minutes: Minutes
    markdown: str
    dropped: list[ActionItem]  # action items whose timestamp is not in the transcript
    usage: Usage
    calls: int


# --- parsing -------------------------------------------------------------------------


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [s for v in value if (s := _str(v))]


def normalize_hms(value: str) -> str | None:
    m = _HMS_RE.match(value.strip())
    if not m:
        return None
    h, mi, s = (int(g) for g in m.groups())
    if h > 23 or mi > 59 or s > 59:
        return None
    return f"{h:02d}:{mi:02d}:{s:02d}"


def parse_minutes(text: str) -> Minutes:
    """Parse the model's JSON answer; tolerates code fences and text around the object."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise MinutesParseError("no JSON object in model output")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise MinutesParseError(f"invalid JSON in model output: {e}") from e
    if not isinstance(data, dict):
        raise MinutesParseError("model output is not a JSON object")

    actions = []
    for raw in data.get("acoes") or []:
        if not isinstance(raw, dict) or not (desc := _str(raw.get("descricao"))):
            continue
        actions.append(
            ActionItem(
                description=desc,
                owner=_str(raw.get("responsavel")),
                due=_str(raw.get("prazo")),
                ref=_str(raw.get("ref")) or "",
            )
        )
    return Minutes(
        summary=_str(data.get("resumo")) or "",
        decisions=_str_list(data.get("decisoes")),
        actions=actions,
        open_questions=_str_list(data.get("perguntas_abertas")),
    )


def validate_actions(minutes: Minutes, known: set[str]) -> tuple[Minutes, list[ActionItem]]:
    """Keep only action items whose timestamp is a line of the transcript."""
    kept, dropped = [], []
    for item in minutes.actions:
        hms = normalize_hms(item.ref)
        if hms is not None and hms in known:
            kept.append(ActionItem(item.description, item.owner, item.due, hms))
        else:
            dropped.append(item)
            log.warning(
                "dropped action item with timestamp not in transcript: ref=%r %r",
                item.ref,
                item.description,
            )
    return Minutes(minutes.summary, minutes.decisions, kept, minutes.open_questions), dropped


# --- rendering -----------------------------------------------------------------------


def render_minutes(minutes: Minutes) -> str:
    def bullets(items: list[str], empty: str) -> str:
        return "\n".join(f"- {i}" for i in items) if items else f"- {empty}"

    actions = []
    for a in minutes.actions:
        owner = a.owner or "sem responsável definido"
        due = f" · prazo: {a.due}" if a.due else ""
        actions.append(f"- [ ] {a.description} — responsável: {owner}{due} · [{a.ref}]")
    return "\n\n".join(
        [
            f"### Resumo\n\n{minutes.summary or 'Sem resumo.'}",
            f"### Decisões\n\n{bullets(minutes.decisions, 'Nenhuma decisão registrada.')}",
            "### Itens de ação\n\n"
            + ("\n".join(actions) if actions else "- Nenhum item de ação registrado."),
            "### Perguntas em aberto\n\n"
            + bullets(minutes.open_questions, "Nenhuma pergunta em aberto."),
        ]
    )


# --- generation ----------------------------------------------------------------------


class _Generator:
    def __init__(self, model: TextModel, context_tokens: int, max_tokens: int) -> None:
        self.model = model
        self.max_tokens = max_tokens
        # Room for the system prompt and the answer.
        self.budget = max(1_000, context_tokens - max_tokens - 2_000)
        self.usages: list[Usage] = []

    def _call(self, system: str, user: str) -> Minutes:
        text, usage = self.model.complete(system, user, self.max_tokens)
        self.usages.append(usage)
        return parse_minutes(text)

    def run(self, lines: list[TranscriptLine]) -> Minutes:
        chunks = chunk_lines(lines, self.budget)
        partials = [
            self._call(prompts.MINUTES_SYSTEM, prompts.minutes_user(render(c))) for c in chunks
        ]
        return self.reduce(partials)

    def reduce(self, partials: list[Minutes]) -> Minutes:
        if len(partials) == 1:
            return partials[0]
        payload = "[" + ",".join(p.to_json() for p in partials) + "]"
        if estimate_tokens(payload) <= self.budget or len(partials) == 2:
            return self._call(prompts.REDUCE_SYSTEM, prompts.reduce_user(payload))
        mid = len(partials) // 2
        return self.reduce([self.reduce(partials[:mid]), self.reduce(partials[mid:])])


def generate_minutes(
    lines: list[TranscriptLine],
    model: TextModel,
    *,
    context_tokens: int = DEFAULT_CONTEXT_TOKENS,
    max_tokens: int = DEFAULT_MINUTES_MAX_TOKENS,
) -> MinutesResult:
    """Minutes for already-parsed transcript lines (no file I/O)."""
    if not lines:
        minutes = Minutes(summary="Nenhuma fala registrada nesta reunião.")
        return MinutesResult(minutes, render_minutes(minutes), [], Usage(), 0)
    gen = _Generator(model, context_tokens, max_tokens)
    raw = gen.run(lines)
    minutes, dropped = validate_actions(raw, {line.hms for line in lines})
    return MinutesResult(
        minutes, render_minutes(minutes), dropped, add_usage(gen.usages), len(gen.usages)
    )


def write_minutes(
    meeting_dir: Path,
    model: TextModel,
    *,
    context_tokens: int = DEFAULT_CONTEXT_TOKENS,
    max_tokens: int = DEFAULT_MINUTES_MAX_TOKENS,
    day: datetime | None = None,
) -> MinutesResult:
    """Generate the minutes from ``transcript.md`` and write ``## Ata`` in ``summary.md``.

    Calling it again replaces the previous ``## Ata``; epoch sections are kept.
    """
    lines = read_transcript(meeting_dir / TRANSCRIPT_FILE, day=day or datetime.now().astimezone())
    result = generate_minutes(lines, model, context_tokens=context_tokens, max_tokens=max_tokens)
    write_minutes_section(meeting_dir / SUMMARY_FILE, result.markdown)
    return result
