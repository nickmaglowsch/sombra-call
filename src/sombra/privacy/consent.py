"""Consent gate: the user confirms participants were told before capture starts.

``require_consent`` shows a PT-BR notice to paste into the call chat, asks for an
explicit "sim", and writes who/when to ``consent.json`` in the meeting folder. A
refusal raises ``ConsentRefusedError``; the orchestrator must not start capture then.
"""

from __future__ import annotations

import getpass
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

CONSENT_FILE = "consent.json"

DEFAULT_NOTICE_PT_BR = (
    "Aviso: esta reunião está sendo gravada e transcrita pelo Sombra, um assistente "
    "que roda no meu computador. Trechos da conversa e da tela compartilhada podem ser "
    "enviados a um provedor de IA para gerar respostas. Se você não concorda, me avise "
    "agora e eu desligo."
)

CONFIRM_PROMPT = 'Você colou o aviso no chat e os participantes foram informados? Digite "sim": '

_YES = {"sim", "s", "yes", "y"}


class ConsentRefusedError(RuntimeError):
    """The user did not confirm that participants were told. Capture must not start."""


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    confirmed_by: str  # local OS user who confirmed
    confirmed_at: datetime
    notice: str  # the exact text shown

    def to_json(self) -> str:
        data = asdict(self)
        data["confirmed_at"] = self.confirmed_at.isoformat()
        return json.dumps(data, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> ConsentRecord:
        data = json.loads(text)
        return cls(
            confirmed_by=str(data["confirmed_by"]),
            confirmed_at=datetime.fromisoformat(data["confirmed_at"]),
            notice=str(data["notice"]),
        )


def _stdin_confirm(notice: str) -> bool:
    print("\n" + notice + "\n")  # noqa: T201 - this is the interactive prompt itself
    try:
        answer = input(CONFIRM_PROMPT)
    except EOFError:
        return False
    return answer.strip().lower() in _YES


def load_consent(meeting_dir: Path) -> ConsentRecord | None:
    """The consent already recorded for this meeting, or None if missing/unreadable."""
    path = meeting_dir / CONSENT_FILE
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return ConsentRecord.from_json(path.read_text(encoding="utf-8"))
    except (ValueError, KeyError, TypeError):
        return None


def require_consent(
    meeting_dir: Path,
    interactive: bool = True,
    *,
    notice: str = DEFAULT_NOTICE_PT_BR,
    confirm: Callable[[str], bool] = _stdin_confirm,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    user: str | None = None,
) -> ConsentRecord:
    """Ask for (or, non-interactively, require an existing) consent confirmation.

    ``interactive=True`` always asks, even if a record exists (each start of capture is
    a new chance for someone to object). ``interactive=False`` never prompts: it
    returns the recorded consent or raises ``ConsentRefusedError``.
    """
    if not interactive:
        existing = load_consent(meeting_dir)
        if existing is None:
            raise ConsentRefusedError(
                f"no consent recorded in {meeting_dir / CONSENT_FILE}; run interactively first"
            )
        return existing

    if not confirm(notice):
        raise ConsentRefusedError("participants were not confirmed as informed; not starting")

    record = ConsentRecord(
        confirmed_by=user if user is not None else getpass.getuser(),
        confirmed_at=now(),
        notice=notice,
    )
    meeting_dir.mkdir(parents=True, exist_ok=True)
    path = meeting_dir / CONSENT_FILE
    if path.is_symlink():
        raise ConsentRefusedError(f"refusing to write through a symlink: {path}")
    tmp = path.with_name(CONSENT_FILE + ".tmp")
    tmp.write_text(record.to_json() + "\n", encoding="utf-8")
    tmp.replace(path)
    return record
