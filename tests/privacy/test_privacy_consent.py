import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sombra.privacy import consent as consent_mod
from sombra.privacy.consent import (
    CONSENT_FILE,
    DEFAULT_NOTICE_PT_BR,
    ConsentRecord,
    ConsentRefusedError,
    load_consent,
    require_consent,
)

NOW = datetime(2026, 9, 29, 14, 30, tzinfo=UTC)


def test_notice_is_pt_br_and_mentions_recording_transcription_and_ai() -> None:
    text = DEFAULT_NOTICE_PT_BR.lower()
    assert "gravada" in text
    assert "transcrita" in text
    assert "provedor de ia" in text


def test_refusal_blocks_start_and_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(ConsentRefusedError):
        require_consent(tmp_path, confirm=lambda notice: False)
    assert not (tmp_path / CONSENT_FILE).exists()


def test_confirmation_writes_record(tmp_path: Path) -> None:
    shown: list[str] = []

    def confirm(notice: str) -> bool:
        shown.append(notice)
        return True

    rec = require_consent(tmp_path / "m", confirm=confirm, now=lambda: NOW, user="nick")
    assert shown == [DEFAULT_NOTICE_PT_BR]
    assert rec == ConsentRecord(confirmed_by="nick", confirmed_at=NOW, notice=DEFAULT_NOTICE_PT_BR)
    data = json.loads((tmp_path / "m" / CONSENT_FILE).read_text(encoding="utf-8"))
    assert data == {
        "confirmed_by": "nick",
        "confirmed_at": "2026-09-29T14:30:00+00:00",
        "notice": DEFAULT_NOTICE_PT_BR,
    }
    assert load_consent(tmp_path / "m") == rec
    assert not (tmp_path / "m" / (CONSENT_FILE + ".tmp")).exists()


def test_default_user_is_os_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(consent_mod.getpass, "getuser", lambda: "someone")
    rec = require_consent(tmp_path, confirm=lambda n: True, now=lambda: NOW)
    assert rec.confirmed_by == "someone"


def test_non_interactive_requires_existing_record(tmp_path: Path) -> None:
    with pytest.raises(ConsentRefusedError):
        require_consent(tmp_path, interactive=False)
    rec = require_consent(tmp_path, confirm=lambda n: True, now=lambda: NOW, user="u")

    def never(notice: str) -> bool:
        raise AssertionError("must not prompt")

    assert require_consent(tmp_path, interactive=False, confirm=never) == rec


def test_corrupt_record_counts_as_no_consent(tmp_path: Path) -> None:
    (tmp_path / CONSENT_FILE).write_text("{not json", encoding="utf-8")
    assert load_consent(tmp_path) is None
    with pytest.raises(ConsentRefusedError):
        require_consent(tmp_path, interactive=False)


def test_symlinked_record_is_ignored_and_never_written_through(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("original", encoding="utf-8")
    meeting = tmp_path / "m"
    meeting.mkdir()
    (meeting / CONSENT_FILE).symlink_to(outside)
    assert load_consent(meeting) is None
    with pytest.raises(ConsentRefusedError):
        require_consent(meeting, confirm=lambda n: True, now=lambda: NOW, user="u")
    assert outside.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize(
    ("answer", "ok"), [("sim", True), (" SIM ", True), ("s", True), ("não", False), ("", False)]
)
def test_stdin_confirm(
    answer: str, ok: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    assert consent_mod._stdin_confirm("AVISO") is ok
    assert "AVISO" in capsys.readouterr().out


def test_stdin_confirm_eof_is_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    def eof(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert consent_mod._stdin_confirm("AVISO") is False


def test_planted_tmp_symlink_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("original", encoding="utf-8")
    meeting = tmp_path / "m"
    meeting.mkdir()
    (meeting / (CONSENT_FILE + ".tmp")).symlink_to(outside)
    rec = require_consent(meeting, confirm=lambda n: True, now=lambda: NOW, user="u")
    assert outside.read_text(encoding="utf-8") == "original"
    assert load_consent(meeting) == rec
