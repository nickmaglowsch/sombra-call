"""A small synthetic PT-BR meeting for ``sombra ask`` tests (no real meeting data)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sombra.contracts import Channel, FrameMarker, SpeechLine, TimelineEntry
from sombra.store import create_meeting

STARTED = datetime(2026, 9, 29, 14, 30, 0).astimezone()

_SCRIPT: list[tuple[int, str, str]] = [
    (5, "OUTROS", "bom dia, vamos começar a daily do time X"),
    (40, "OUTROS", "Nick, sobre o beta, qual prazo a gente combinou com o cliente?"),
    (62, "EU", "combinamos que o beta sai na sexta, dia 3 de outubro"),
    (80, "OUTROS", "fechado, beta na sexta dia 3 então"),
    (130, "TELA", "f0001|Zoom - Dashboard de churn"),
    (135, "OUTROS", "o churn de setembro fechou em 2,4 por cento, caiu meio ponto"),
    (180, "EU", "a migração do banco fica com a Ana, ela começa na segunda"),
    (210, "OUTROS", "combinado, a Ana cuida da migração do banco"),
    (240, "OUTROS", "a próxima daily é quinta às dez, valeu pessoal"),
]


def entries(started: datetime = STARTED) -> list[TimelineEntry]:
    out: list[TimelineEntry] = []
    for offset, who, text in _SCRIPT:
        ts = started + timedelta(seconds=offset)
        if who == "TELA":
            frame_id, title = text.split("|", 1)
            out.append(FrameMarker(ts=ts, frame_id=frame_id, window_title=title))
        else:
            channel = Channel.ME if who == "EU" else Channel.OTHERS
            out.append(SpeechLine(ts=ts, channel=channel, text=text))
    return out


def make_meeting(
    root: Path, name: str = "Daily time X", *, started: datetime = STARTED, summary: str = ""
) -> Path:
    """A meeting folder with the script above in ``transcript.md`` and one frame on disk."""
    meeting = create_meeting(
        root, name, aliases=("Nick",), started_at=started, header="# Daily time X"
    )
    with (meeting / "transcript.md").open("a", encoding="utf-8") as f:
        for entry in entries(started):
            f.write(entry.to_line() + "\n")
    (meeting / "frames" / "f0001.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9")
    if summary:
        (meeting / "summary.md").write_text(summary, encoding="utf-8")
    return meeting


def transcript_times(meeting: Path) -> set[str]:
    """Every ``HH:MM:SS`` that starts a line of the meeting's transcript."""
    lines = (meeting / "transcript.md").read_text(encoding="utf-8").splitlines()
    return {line[1:9] for line in lines if line.startswith("[") and line[9:10] == "]"}
