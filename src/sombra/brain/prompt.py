"""Prompt assembly: what the model sees on each trigger (PRD C1, C3, content side of C6).

Owns the system prompt (``system_prompt_pt.md``), the *stable prefix* and the
*ephemeral tail*. Everything here is pure and backend-agnostic: it returns plain
dicts in the Anthropic Messages content-block shape, so the Claude adapter and any
other backend share one implementation. Nothing here calls an API.

Invariants (CONTRIBUTING.md gate 4):

- **Stable prefix.** Within one summary epoch, ``PrefixBuilder.blocks()`` at call N is
  a byte-for-byte prefix of ``blocks()`` at call N+1; only the moving
  ``cache_control`` marker on the last block differs. Blocks are frozen once
  returned; the ``context/`` index is read once, at construction.
- **Images only in the tail.** ``build_tail`` is stateless; its output (frames,
  the 60 s window, the question) never enters ``PrefixBuilder``.
- **Meeting content is data.** Transcript, window titles, summary, context files and
  the question are wrapped in ``<dados fonte="...">`` delimiters with ``&``, ``<`` and
  ``>`` escaped (plus ``"`` in attributes), so content cannot close or forge a delimiter.

Prefix layout (``blocks()``)::

    [0] system prompt                       -> request ``system``
    [1] context/ index + small text files   (fixed cache breakpoint)
    [2] epoch summary                       (only after ``start_epoch``)
    [3..] transcript chunks, one per flush  (moving cache breakpoint on the last)

``render_request`` sends ``prefix[0]`` as the top-level ``system`` and the rest of
the prefix followed by the tail as the single user message, so untrusted meeting
content never gets system-role authority.
"""

from __future__ import annotations

import base64
import copy
import html
import re
from collections.abc import Sequence
from importlib import resources
from pathlib import Path
from string import Template
from typing import Any

from sombra.contracts import AutonomyLevel, TimelineEntry, TriggerEvent
from sombra.contracts.timeline import FRAME_ID_RE

MAX_TAIL_FRAMES = 3
"""Hard cap on images per call (PRD S5/C1)."""

CHARS_PER_TOKEN = 4
IMAGE_TOKEN_ESTIMATE = 1600
"""Rough per-image cost: (w*h)/750 for a ~1280x900 frame, rounded up."""

DEFAULT_MAX_INLINE_FILE_BYTES = 20_000
DEFAULT_MAX_INLINE_TOTAL_BYTES = 100_000
TEXT_SUFFIXES = frozenset(
    {".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".rst", ".org"}
)

FRAME_REQUEST_PREFIX = "PRECISO_DA_TELA"
"""The model answers ``PRECISO_DA_TELA f0123`` to ask for a frame (see the system prompt)."""

_FRAME_REQUEST_RE = re.compile(rf"^\s*{FRAME_REQUEST_PREFIX}\s+(f\d{{4,}})\s*$")

_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}

_IMAGE_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}

_LEVEL_RULES = {
    AutonomyLevel.L0: (
        "Nível L0 (só registro): normalmente você não é chamado. Se for, responda apenas "
        '"preciso confirmar e te retorno".'
    ),
    AutonomyLevel.L1: (
        "Nível L1 (copiloto): sua resposta é uma sugestão privada, vista só por "
        "$user_name, que decide o que falar."
    ),
    AutonomyLevel.L2: (
        "Nível L2: $user_name aprova ou edita sua resposta antes de ela ser dita na reunião."
    ),
    AutonomyLevel.L3: (
        "Nível L3: sua resposta vai direto para a reunião, sem revisão de $user_name. "
        "Na dúvida, diga que precisa confirmar."
    ),
}


# --- escaping and delimiters ------------------------------------------------------------


def escape_data(text: str, *, quote: bool = False) -> str:
    """Escape ``&``, ``<``, ``>`` (and ``"`` with ``quote``) so content cannot forge a data tag."""
    return html.escape(text, quote=quote)


def wrap_data(source: str, content: str, **attrs: str) -> str:
    """Wrap untrusted content in ``<dados fonte="source" ...>`` delimiters (escaped)."""
    pairs = {"fonte": source, **attrs}.items()
    rendered = "".join(f' {key}="{escape_data(value, quote=True)}"' for key, value in pairs)
    return f"<dados{rendered}>\n{escape_data(content)}\n</dados>"


def _text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _one_line(value: str) -> str:
    return " ".join(value.split())


# --- system prompt ----------------------------------------------------------------------


def _template() -> str:
    return resources.files("sombra.brain").joinpath("system_prompt_pt.md").read_text("utf-8")


def system_prompt(
    user_name: str,
    aliases: Sequence[str],
    allowed_topics: Sequence[str],
    level: AutonomyLevel,
) -> str:
    """Render the PT-BR system prompt (C3). Deterministic: same inputs, same bytes."""
    name = _one_line(user_name)
    alias_list: list[str] = []
    for alias in (_one_line(a) for a in aliases):
        if alias and alias.casefold() != name.casefold() and alias not in alias_list:
            alias_list.append(alias)
    topics = [_one_line(t) for t in allowed_topics if _one_line(t)]
    if topics:
        topics_rule = "Responda apenas sobre estes temas:\n" + "\n".join(f"- {t}" for t in topics)
    else:
        topics_rule = (
            "Nenhuma lista de temas foi definida: fale apenas do que aparece nesta reunião "
            "e nos arquivos de contexto."
        )
    level_rule = Template(_LEVEL_RULES[level]).substitute(user_name=name)
    return (
        Template(_template())
        .substitute(
            user_name=name,
            aliases_clause=f" (também chamado de {', '.join(alias_list)})" if alias_list else "",
            topics_rule=topics_rule,
            level_rule=level_rule,
        )
        .rstrip("\n")
    )


def parse_frame_request(answer: str) -> str | None:
    """Return the frame id if the model's answer is a ``PRECISO_DA_TELA fNNNN`` request."""
    m = _FRAME_REQUEST_RE.match(answer)
    return m[1] if m else None


# --- stable prefix ----------------------------------------------------------------------


def _context_index(meeting_dir: Path, max_file_bytes: int, max_total_bytes: int) -> str:
    root = meeting_dir / "context"
    files: list[Path] = []
    if root.is_dir():
        files = sorted(
            (
                p
                for p in root.rglob("*")
                if p.is_file()
                and not p.is_symlink()  # never follow links out of the meeting folder
                and not any(part.startswith(".") for part in p.relative_to(root).parts)
            ),
            key=lambda p: p.relative_to(root).as_posix(),
        )
    if not files:
        return wrap_data("contexto", "Nenhum arquivo em context/.")

    listing: list[str] = []
    inlined: list[str] = []
    budget = max_total_bytes
    for path in files:
        rel = f"context/{path.relative_to(root).as_posix()}"
        size = path.stat().st_size
        text: str | None = None
        if path.suffix.lower() in TEXT_SUFFIXES and size <= min(max_file_bytes, budget):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = None
        if text is None:
            listing.append(f"- {rel} ({size} bytes, não incluído; leia o arquivo se precisar)")
        else:
            budget -= size
            listing.append(f"- {rel} ({size} bytes, incluído abaixo)")
            inlined.append(wrap_data("arquivo", text, nome=rel))
    index = wrap_data("contexto", "Arquivos em context/:\n" + "\n".join(listing))
    return "\n\n".join([index, *inlined])


class PrefixBuilder:
    """The stable, cacheable prompt prefix for one meeting. Append-only within an epoch.

    ``add_transcript`` buffers entries; ``blocks()`` seals the buffer into one new
    frozen transcript block and returns the whole prefix. So each call adds at most
    one block, which keeps the moving cache breakpoint within the API's lookback.
    """

    def __init__(
        self,
        meeting_dir: Path,
        system: str,
        *,
        max_inline_file_bytes: int = DEFAULT_MAX_INLINE_FILE_BYTES,
        max_inline_total_bytes: int = DEFAULT_MAX_INLINE_TOTAL_BYTES,
    ) -> None:
        self._system = _text_block(system)
        # Read once: context files that change mid-meeting must not move the prefix.
        self._context = _text_block(
            _context_index(Path(meeting_dir), max_inline_file_bytes, max_inline_total_bytes)
        )
        self._epoch = 0
        self._summary: dict[str, Any] | None = None
        self._chunks: list[dict[str, Any]] = []
        self._pending: list[str] = []

    @property
    def epoch(self) -> int:
        return self._epoch

    def add_transcript(self, entries: Sequence[TimelineEntry]) -> None:
        self._pending.extend(entry.to_line() for entry in entries)

    def start_epoch(self, summary_md: str) -> None:
        """Replace the transcript so far with its summary: the planned cache miss (C4).

        Entries buffered but not yet sealed by ``blocks()`` are covered by the summary
        too, so they are dropped with the rest.
        """
        self._epoch += 1
        self._summary = _text_block(wrap_data("resumo", summary_md, epoca=str(self._epoch)))
        self._chunks = []
        self._pending = []

    def blocks(self) -> list[dict[str, Any]]:
        """The prefix as content blocks, ``cache_control`` on the context and last blocks.

        Returns fresh copies: callers may mutate them without touching the builder.
        """
        if self._pending:
            self._chunks.append(_text_block(wrap_data("transcricao", "\n".join(self._pending))))
            self._pending = []
        out = [copy.deepcopy(self._system), copy.deepcopy(self._context)]
        if self._summary is not None:
            out.append(copy.deepcopy(self._summary))
        out.extend(copy.deepcopy(chunk) for chunk in self._chunks)
        out[1]["cache_control"] = dict(_CACHE_CONTROL)  # system+context: survives epochs
        out[-1]["cache_control"] = dict(_CACHE_CONTROL)  # moving marker
        return out


# --- ephemeral tail ---------------------------------------------------------------------


def _frame_label(path: Path) -> str:
    return path.stem if FRAME_ID_RE.match(path.stem) else path.name


def build_tail(trigger: TriggerEvent, frame_paths: Sequence[Path]) -> list[dict[str, Any]]:
    """The per-call tail: last ~60 s, 0-3 images (base64) and the question. Never cached.

    Raises ``ValueError`` for more than ``MAX_TAIL_FRAMES`` frames or an unknown image type.
    """
    if len(frame_paths) > MAX_TAIL_FRAMES:
        raise ValueError(f"at most {MAX_TAIL_FRAMES} frames per call, got {len(frame_paths)}")
    window = "\n".join(entry.to_line() for entry in trigger.window) or "(sem falas recentes)"
    tail: list[dict[str, Any]] = [
        _text_block("Últimos ~60 segundos da reunião:\n" + wrap_data("janela", window))
    ]
    for path in frame_paths:
        media_type = _IMAGE_MEDIA_TYPES.get(Path(path).suffix.lower())
        if media_type is None:
            raise ValueError(f"unsupported frame type: {path}")
        tail.append(_text_block(f"Imagem da tela {_frame_label(Path(path))}:"))
        tail.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(Path(path).read_bytes()).decode("ascii"),
                },
            }
        )
    tail.append(
        _text_block(
            "Pergunta feita a você (chamado como "
            f'"{escape_data(_one_line(trigger.matched_alias), quote=True)}"):\n'
            + wrap_data("pergunta", trigger.question)
            + "\nEscreva a resposta que será dita, seguindo as regras do sistema."
        )
    )
    return tail


# --- request ----------------------------------------------------------------------------


def render_request(
    prefix: list[dict[str, Any]],
    tail: list[dict[str, Any]],
    *,
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    """Messages API request body: ``prefix[0]`` as ``system``, the rest + tail as one user turn."""
    if not prefix:
        raise ValueError("prefix must start with the system block")
    content = [copy.deepcopy(b) for b in (*prefix[1:], *tail)]
    if not content:
        raise ValueError("request has no user content")
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": [copy.deepcopy(prefix[0])],
        "messages": [{"role": "user", "content": content}],
    }


def estimate_tokens(blocks: Sequence[dict[str, Any]]) -> int:
    """Cheap input-token estimate for cost logging: chars/4 for text, a flat rate per image."""
    total = 0
    for block in blocks:
        if block.get("type") == "image":
            total += IMAGE_TOKEN_ESTIMATE
        else:
            total += -(-len(str(block.get("text", ""))) // CHARS_PER_TOKEN)
    return total


def estimate_request_tokens(request: dict[str, Any]) -> int:
    """``estimate_tokens`` over a ``render_request`` body (system + all message content)."""
    blocks: list[dict[str, Any]] = list(request.get("system", []))
    for message in request.get("messages", []):
        content = message.get("content", [])
        blocks.extend(content if isinstance(content, list) else [_text_block(str(content))])
    return estimate_tokens(blocks)
