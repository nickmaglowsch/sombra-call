"""#47: every backend is selectable, and ``start`` / ``replay`` / ``ask`` / ``minutes`` use it.

The CLIs are scripted (no real ``claude`` / ``codex``), the keychain is a dict and the
summary model is a fake, so this runs in CI.
"""

from __future__ import annotations

import argparse
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from ask_fixture import make_meeting

from fakes import FakeBrain
from sombra.brain.claude import ClaudeBrain
from sombra.brain.claude_code import ClaudeCodeBrain
from sombra.brain.codex import CodexBrain, ProcessResult
from sombra.cli import build_parser
from sombra.config import BrainConfig, SummaryConfig, UserConfig, UserIdentity
from sombra.contracts import AutonomyLevel, Usage
from sombra.orchestrator import ask as ask_mod
from sombra.orchestrator import commands, smoke, wiring
from sombra.orchestrator.ask import (
    ApiKeyMissingError,
    FrameGate,
    FramelessView,
    ask,
    ask_brain,
    claude_code_brain,
)
from sombra.orchestrator.session import local_now
from sombra.orchestrator.wiring import (
    MissingKeyError,
    agent_key,
    backend_model,
    build_agent_brain,
    build_summary_model,
    summary_model_for,
)
from sombra.summary import AnthropicTextModel, ClaudeCliTextModel, CodexCliTextModel


def _keys(**stored: str) -> wiring.Keys:
    return lambda provider: (lambda: stored[provider]) if provider in stored else None


def _cfg(root: Path, backend: str = "claude-api", auth: str | None = None, **kw: Any) -> UserConfig:
    return UserConfig(
        meetings_root=root,
        user=UserIdentity(name="Nick", aliases=("Nico",)),
        brain=BrainConfig(backend, auth),
        **kw,
    )


# --- model aliases and keys per backend ----------------------------------------------------


@pytest.mark.parametrize(
    ("backend", "name", "expected"),
    [
        ("claude-api", "sonnet", "claude-sonnet-5-5"),
        ("claude", "haiku", "claude-haiku-4-5"),  # the pre-#47 name
        ("claude-api", "default", None),
        ("claude-code", "sonnet", "sonnet"),  # the CLI resolves its own aliases
        ("claude-code", "claude-opus-5-5", "claude-opus-5-5"),
        ("claude-code", " Default ", None),
        ("codex", "sonnet", None),  # a Claude alias means nothing to Codex
        ("codex", "claude-opus-5-5", None),
        ("codex", "gpt-5-codex", "gpt-5-codex"),
        ("codex", "", None),
    ],
)
def test_backend_model(backend: str, name: str, expected: str | None) -> None:
    assert backend_model(backend, name) == expected


def test_agent_key_per_backend_and_auth() -> None:
    both = _keys(anthropic="sk-a", openai="sk-o")
    assert agent_key(BrainConfig("claude-code"), both) is None  # never: billing stays on the plan
    assert agent_key(BrainConfig("codex", "subscription"), both) is None
    key = agent_key(BrainConfig("claude-api"), both)
    assert key is not None and key() == "sk-a"
    key = agent_key(BrainConfig("codex", "api-key"), both)
    assert key is not None and key() == "sk-o"
    legacy = agent_key(BrainConfig("codex"), both)  # no auth: the stored key if any
    assert legacy is not None and legacy() == "sk-o"
    assert agent_key(BrainConfig("codex"), _keys()) is None  # ...else `codex login`
    for brain, provider in (
        (BrainConfig("claude-api"), "anthropic"),
        (BrainConfig("codex", "api-key"), "openai"),
    ):
        with pytest.raises(MissingKeyError, match=f"sombra auth set {provider}"):
            agent_key(brain, _keys())


def test_summary_model_per_backend() -> None:
    model, why = build_summary_model("claude-code", "haiku", _keys())
    assert isinstance(model, ClaudeCliTextModel) and model.name == "haiku" and why is None
    model, _ = build_summary_model("codex", "haiku", _keys(openai="sk-o"))
    assert isinstance(model, CodexCliTextModel) and model.name == "codex-default"
    model, _ = build_summary_model("claude-api", "haiku", _keys(anthropic="sk-a"))
    assert isinstance(model, AnthropicTextModel) and model.name == "claude-haiku-4-5"
    model, why = build_summary_model("claude-api", "haiku", _keys())
    assert model is None and why is not None and "no anthropic key" in why
    model, why = build_summary_model(None, "haiku", _keys())
    assert model is None and why is not None and "none" in why


def test_summary_follows_the_agent_unless_set(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, "claude-code")
    assert isinstance(summary_model_for(cfg, _keys())[0], ClaudeCliTextModel)
    cfg = _cfg(tmp_path, "claude-code", summary=SummaryConfig("codex"))
    assert isinstance(summary_model_for(cfg, _keys())[0], CodexCliTextModel)


def test_live_brain_for_every_backend() -> None:
    kw: dict[str, Any] = {"user_name": "Nick", "aliases": ["Nico"], "allowed_topics": ()}
    code = build_agent_brain("claude-code", None, level=AutonomyLevel.L2, model="opus", **kw)
    assert isinstance(code, ClaudeCodeBrain) and code.settings.model == "opus"
    api = build_agent_brain("claude-api", lambda: "k", level=AutonomyLevel.L2, model="opus", **kw)
    assert isinstance(api, ClaudeBrain) and api.settings.model == "claude-opus-5-5"
    codex = build_agent_brain("codex", None, level=AutonomyLevel.L2, model="opus", **kw)
    assert isinstance(codex, CodexBrain) and codex.settings.model is None


# --- sombra ask ------------------------------------------------------------------------------


def test_ask_brain_per_backend(tmp_path: Path) -> None:
    meeting = make_meeting(tmp_path / "meetings")
    keys = _keys(anthropic="sk-a", openai="sk-o")
    api = ask_brain(meeting, _cfg(tmp_path), frames=False, keys=keys)
    assert isinstance(api, ClaudeBrain) and isinstance(api._client, FrameGate)

    code = ask_brain(meeting, _cfg(tmp_path, "claude-code"), frames=False, keys=keys, model="opus")
    assert isinstance(code, FramelessView) and isinstance(code.inner, ClaudeCodeBrain)
    assert code.inner.settings.model == "opus" and code.inner.settings.timeout_s == 120.0
    with_frames = ask_brain(meeting, _cfg(tmp_path, "claude-code"), frames=True, keys=keys)
    assert isinstance(with_frames, ClaudeCodeBrain)

    sub = ask_brain(meeting, _cfg(tmp_path, "codex", "subscription"), frames=True, keys=keys)
    assert isinstance(sub, CodexBrain) and sub._api_key is None  # `codex login` pays
    keyed = ask_brain(meeting, _cfg(tmp_path, "codex", "api-key"), frames=True, keys=keys)
    assert isinstance(keyed, CodexBrain) and keyed._api_key is not None
    assert keyed._api_key() == "sk-o"


@pytest.mark.parametrize(("backend", "auth"), [("claude-api", None), ("codex", "api-key")])
def test_ask_brain_without_the_key_fails_before_starting(
    tmp_path: Path, backend: str, auth: str | None
) -> None:
    meeting = make_meeting(tmp_path / "meetings")
    with pytest.raises(ApiKeyMissingError, match="sombra auth set"):
        ask_brain(meeting, _cfg(tmp_path, backend, auth), frames=False, keys=_keys())


async def test_frameless_view_hides_frames_and_cleans_up(tmp_path: Path) -> None:
    meeting = make_meeting(tmp_path / "meetings", summary="# Resumo\n")
    (meeting / "context" / "frames").mkdir()  # only the top-level frames/ is dropped
    inner = FakeBrain()
    view = FramelessView(inner)
    result = await ask(view, meeting, "o prazo?", frames=False, clock=local_now)
    assert result.response.text == "resposta 1"
    copy = inner.meeting_dir
    assert copy is not None and copy != meeting and copy.name == meeting.name
    assert inner.closed and not copy.exists()  # removed after the question
    assert (meeting / "frames" / "f0001.jpg").exists()  # the original is untouched


def _listing(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


async def test_frameless_view_copy_has_no_frames(tmp_path: Path) -> None:
    meeting = make_meeting(tmp_path / "meetings", summary="# Resumo\n")
    (meeting / "context" / "frames").mkdir()
    seen: dict[str, set[str]] = {}

    class Peek(FakeBrain):
        async def start(self, meeting_dir: Path) -> None:
            await super().start(meeting_dir)
            seen["files"] = _listing(meeting_dir)

    view = FramelessView(Peek())
    await view.start(meeting)
    await view.close()
    assert "transcript.md" in seen["files"] and "summary.md" in seen["files"]
    assert "context/frames" in seen["files"]
    assert not any(f == "frames" or f.startswith("frames/") for f in seen["files"])


async def test_frameless_view_cleans_up_when_close_fails(tmp_path: Path) -> None:
    meeting = make_meeting(tmp_path / "meetings")

    class Broken(FakeBrain):
        async def close(self) -> None:
            raise RuntimeError("boom")

    inner = Broken()
    view = FramelessView(inner)
    await view.start(meeting)
    assert inner.meeting_dir is not None and inner.meeting_dir.exists()
    with pytest.raises(RuntimeError, match="boom"):
        await view.close()
    assert not inner.meeting_dir.exists()


def _stream(text: str) -> str:
    events = [
        {"type": "system", "subtype": "init", "tools": ["Read", "Grep", "Glob"],
         "mcp_servers": [], "model": "claude-sonnet-5-5", "permissionMode": "dontAsk",
         "apiKeySource": "none"},
        {"type": "assistant",
         "message": {"model": "claude-sonnet-5-5", "content": [{"type": "text", "text": text}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": text,
         "api_error_status": None, "usage": {"input_tokens": 12, "output_tokens": 3},
         "permission_denials": []},
    ]  # fmt: skip
    return "\n".join(json.dumps(e) for e in events) + "\n"


class ScriptedClaude:
    """``ClaudeCodeRunner`` fake: answers ``--version`` and one scripted run."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.runs: list[tuple[list[str], str, dict[str, str], Path]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        env: Mapping[str, str],
        cwd: Path,
        timeout_s: float,
    ) -> ProcessResult:
        if list(argv[-1:]) == ["--version"]:
            return ProcessResult(0, "2.1.285 (Claude Code)\n", "")
        self.runs.append((list(argv), stdin, dict(env), cwd))
        return ProcessResult(0, _stream(self.answer), "")


async def test_ask_through_claude_code_on_the_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    meeting = make_meeting(tmp_path / "meetings")
    cli = ScriptedClaude("O beta sai na sexta [14:31:02].")
    inner = claude_code_brain(meeting, _cfg(tmp_path, "claude-code"), frames=False, runner=cli)
    result = await ask(
        FramelessView(inner), meeting, "Qual o prazo?", frames=False, clock=local_now
    )
    assert result.response.text == "O beta sai na sexta [14:31:02]."
    [(argv, stdin, env, cwd)] = cli.runs
    assert "ANTHROPIC_API_KEY" not in env  # the subscription pays, never a key
    assert "Qual o prazo?" in stdin and "combinamos que o beta sai na sexta" in stdin
    assert cwd != meeting.resolve() and not (cwd / "frames").exists()  # a frameless copy
    assert "--restricted" in argv


def test_ask_command_uses_the_configured_backend_and_keychain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "meetings"
    make_meeting(root)
    config = tmp_path / "config.toml"
    config.write_text(
        f'meetings_root = "{root}"\n[user]\nname = "Nick"\n[brain]\nbackend = "claude-code"\n',
        encoding="utf-8",
    )
    seen: dict[str, Any] = {}

    def fake_ask_brain(meeting_dir: Path, cfg: UserConfig, **kw: Any) -> FakeBrain:
        seen.update(kw, backend=cfg.brain.backend)
        return FakeBrain()

    monkeypatch.setattr(ask_mod, "ask_brain", fake_ask_brain)
    args = build_parser().parse_args(["ask", "latest", "q?", "--config", str(config)])
    out = io.StringIO()
    assert commands.run(args, out=out, err=io.StringIO()) == 0
    assert out.getvalue() == "resposta 1\n"
    assert seen["backend"] == "claude-code" and seen["keys"] is commands.key_lookup


# --- sombra minutes ----------------------------------------------------------------------------


class MinutesModel:
    name = "fake-summary"

    def complete(self, system: str, user: str, max_tokens: int) -> tuple[str, Usage]:
        body = {"resumo": "Beta na sexta.", "decisoes": ["beta sexta"], "acoes": []}
        return json.dumps(body), Usage(input_tokens=30, output_tokens=8)


def _minutes_config(tmp_path: Path, extra: str = "") -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        f'meetings_root = "{tmp_path / "meetings"}"\n[brain]\nbackend = "claude-code"\n{extra}',
        encoding="utf-8",
    )
    return config


def _minutes_args(config: Path, *extra: str) -> argparse.Namespace:
    return build_parser().parse_args(["minutes", "latest", "--config", str(config), *extra])


def test_minutes_use_the_summary_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    meeting = make_meeting(tmp_path / "meetings")
    built: list[tuple[str | None, str]] = []

    def fake_build(backend: str | None, model: str, keys: Any) -> tuple[Any, None]:
        built.append((backend, model))
        return MinutesModel(), None

    monkeypatch.setattr(wiring, "build_summary_model", fake_build)
    out = io.StringIO()
    code = commands.run_minutes(_minutes_args(_minutes_config(tmp_path)), out=out)
    assert code == 0, out.getvalue()
    assert built == [("claude-code", "haiku")]  # follows [brain]; [models] summary
    assert "Beta na sexta." in (meeting / "summary.md").read_text(encoding="utf-8")
    assert "(fake-summary)" in out.getvalue()

    code = commands.run_minutes(
        _minutes_args(_minutes_config(tmp_path, '[summary]\nbackend = "codex"\n'), "--model", "x"),
        out=io.StringIO(),
    )
    assert code == 0 and built[-1] == ("codex", "x")


def test_minutes_errors(tmp_path: Path) -> None:
    make_meeting(tmp_path / "meetings")
    err = io.StringIO()
    config = _minutes_config(tmp_path, '[summary]\nbackend = "claude-api"\n')
    assert commands.run_minutes(_minutes_args(config), keys=_keys(), err=err) == 2
    assert "no anthropic key" in err.getvalue() and "sombra setup" in err.getvalue()

    err = io.StringIO()
    config = _minutes_config(tmp_path, '[summary]\nbackend = "none"\n')
    assert commands.run_minutes(_minutes_args(config), keys=_keys(), err=err) == 2

    err = io.StringIO()
    args = build_parser().parse_args(
        ["minutes", "retro", "--config", str(_minutes_config(tmp_path))]
    )
    assert commands.run_minutes(args, err=err) == 2 and "no meeting 'retro'" in err.getvalue()


def test_minutes_model_failure_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_meeting(tmp_path / "meetings")

    class Down(MinutesModel):
        def complete(self, system: str, user: str, max_tokens: int) -> tuple[str, Usage]:
            raise RuntimeError("Claude Code is not logged in")

    monkeypatch.setattr(wiring, "build_summary_model", lambda *a: (Down(), None))
    err = io.StringIO()
    assert commands.run_minutes(_minutes_args(_minutes_config(tmp_path)), err=err) == 1
    assert "not logged in" in err.getvalue()


# --- the smoke question ------------------------------------------------------------------------


def test_smoke_asks_through_the_configured_agent_on_a_throwaway_meeting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brains: list[FakeBrain] = []

    def fake_ask_brain(meeting_dir: Path, cfg: UserConfig, **kw: Any) -> FakeBrain:
        transcript = (meeting_dir / "transcript.md").read_text(encoding="utf-8")
        assert smoke.SMOKE_LINE in transcript and kw["frames"] is False
        brains.append(FakeBrain())
        return brains[-1]

    monkeypatch.setattr(smoke, "ask_brain", fake_ask_brain)
    answer = smoke.smoke_answer(_cfg(tmp_path, "claude-code"), "Diga pronto", keys=_keys())
    assert answer == "resposta 1"
    [brain] = brains
    assert brain.closed and brain.meeting_dir is not None and not brain.meeting_dir.exists()
    assert brain.requests[0].trigger.question == "Diga pronto"
