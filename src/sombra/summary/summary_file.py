"""The ``summary.md`` layout: rolling epoch summaries, then the final minutes.

::

    # Resumo da reunião

    ## Época 1 · até 14:55:00
    <compact summary of everything up to 14:55:00>

    ## Época 2 · até 15:20:00
    <rewritten summary of everything up to 15:20:00>

    ## Ata
    <final minutes, see sombra.summary.minutes>

Rules:

- Each epoch section is appended once and never edited. The latest epoch replaces the
  earlier ones as *the* summary: the prefix builder reads only the last one
  (``read_current_epoch``), so the prompt prefix changes only at an epoch boundary.
- ``## Ata`` is always the last section. Regenerating minutes replaces it; epochs
  written after it (not expected) are inserted before it.
- Headings inside a section body are demoted to ``###`` so they cannot be mistaken
  for section boundaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

TITLE = "# Resumo da reunião"
MINUTES_HEADING = "## Ata"
_EPOCH_RE = re.compile(r"^## Época (?P<n>\d+) · até (?P<t>\d{2}:\d{2}:\d{2})$")


@dataclass(frozen=True, slots=True)
class EpochSection:
    epoch: int
    until: str  # HH:MM:SS of the last transcript line covered
    text: str


def _sanitize(body: str) -> str:
    lines = []
    for line in body.strip().splitlines():
        if re.match(r"^#{1,2} ", line):
            line = "###" + line.lstrip("#")
        lines.append(line.rstrip())
    return "\n".join(lines)


def _split(content: str) -> tuple[str, str | None]:
    """Split into (everything before ``## Ata``, the Ata section or None)."""
    lines = content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.rstrip("\n") == MINUTES_HEADING:
            return "".join(lines[:i]), "".join(lines[i:])
    return content, None


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else f"{TITLE}\n"


def append_epoch(path: Path, section: EpochSection) -> None:
    head, minutes = _split(_read(path))
    block = f"## Época {section.epoch} · até {section.until}\n{_sanitize(section.text)}\n"
    head = head.rstrip("\n") + "\n\n" + block
    content = head if minutes is None else head + "\n" + minutes
    path.write_text(content, encoding="utf-8")


def read_epochs(path: Path) -> list[EpochSection]:
    if not path.exists():
        return []
    head, _ = _split(path.read_text(encoding="utf-8"))
    sections: list[EpochSection] = []
    current: tuple[int, str] | None = None
    body: list[str] = []
    for line in [*head.splitlines(), "## end"]:
        m = _EPOCH_RE.match(line)
        if m or line.startswith("## "):
            if current is not None:
                sections.append(EpochSection(current[0], current[1], "\n".join(body).strip()))
            current = (int(m["n"]), m["t"]) if m else None
            body = []
        elif current is not None:
            body.append(line)
    return sections


def read_current_epoch(path: Path) -> EpochSection | None:
    """The latest epoch summary: what the brain puts in the stable prompt prefix."""
    epochs = read_epochs(path)
    return epochs[-1] if epochs else None


def write_minutes_section(path: Path, body: str) -> None:
    head, _ = _split(_read(path))
    path.write_text(
        head.rstrip("\n") + f"\n\n{MINUTES_HEADING}\n\n{_sanitize(body)}\n", encoding="utf-8"
    )


def read_minutes_section(path: Path) -> str | None:
    if not path.exists():
        return None
    _, minutes = _split(path.read_text(encoding="utf-8"))
    if minutes is None:
        return None
    return minutes.removeprefix(MINUTES_HEADING).strip()
