import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from summary_fakes import FakeModel, synthetic_lines

from sombra.cli import build_parser, main
from sombra.summary import commands
from sombra.summary import model as model_mod


def _fake_keyring(monkeypatch: pytest.MonkeyPatch, get: Any) -> None:
    mod = types.ModuleType("keyring")
    mod.get_password = get  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", mod)


def test_minutes_command_is_registered() -> None:
    args = build_parser().parse_args(["minutes", "reuniao-x", "--model", "claude-y"])
    assert args.func is commands._run
    assert args.meeting == "reuniao-x" and args.model == "claude-y"


def test_resolve_meeting(tmp_path: Path) -> None:
    assert commands.resolve_meeting(str(tmp_path)) == tmp_path
    assert commands.resolve_meeting("x", root=tmp_path) == tmp_path / "x"


def test_api_key_prefers_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_keyring(monkeypatch, lambda service, user: f"sk-{service}-{user}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert commands.api_key() == "sk-sombra-anthropic-api-key"


def test_api_key_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(service: str, user: str) -> str:
        raise RuntimeError("no backend")

    _fake_keyring(monkeypatch, broken)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert commands.api_key() == "sk-env"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(RuntimeError, match="no API key"):
        commands.api_key()


def test_run_missing_transcript(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["minutes", str(tmp_path)]) == 2
    assert "no transcript.md" in capsys.readouterr().err


def _patch_model(monkeypatch: pytest.MonkeyPatch, fake: FakeModel, seen: dict[str, Any]) -> None:
    def factory(api_key: Any, *, model: str) -> FakeModel:
        seen["model"] = model
        return fake

    monkeypatch.setattr(model_mod, "AnthropicTextModel", factory)


def test_run_writes_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "transcript.md").write_text("\n".join(synthetic_lines(0.05)), encoding="utf-8")
    answer = {"resumo": "r", "acoes": [{"descricao": "a", "ref": "14:00:04"}]}
    seen: dict[str, Any] = {}
    _patch_model(monkeypatch, FakeModel(lambda s, u: json.dumps(answer)), seen)
    assert main(["minutes", str(tmp_path)]) == 0
    assert seen["model"] == model_mod.DEFAULT_MODEL
    out = capsys.readouterr().out
    assert "1 itens de ação, 0 descartados" in out
    assert "## Ata" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_run_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "transcript.md").write_text("\n".join(synthetic_lines(0.05)), encoding="utf-8")
    seen: dict[str, Any] = {}
    _patch_model(monkeypatch, FakeModel(lambda s, u: "não é json"), seen)
    assert main(["minutes", str(tmp_path), "--model", "claude-z"]) == 1
    assert seen["model"] == "claude-z"
    assert "sombra minutes:" in capsys.readouterr().err
