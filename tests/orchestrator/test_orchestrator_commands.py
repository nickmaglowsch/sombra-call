"""``sombra start`` and ``sombra replay``: argument handling and the flows around the session."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from typing import Any

import pytest
from test_orchestrator_replay import FIXTURE, fake_transcriber

from sombra.cli import build_parser
from sombra.contracts import AutonomyLevel
from sombra.orchestrator import commands
from sombra.orchestrator import replay as commands_replay
from sombra.summary import AnthropicTextModel, ClaudeCliTextModel, CodexCliTextModel


def parse(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(list(argv))


def write_config(
    tmp_path: Path, name: str = "Maria", level: str = "L2", backend: str = "claude"
) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'meetings_root = "{tmp_path / "meetings"}"\nautonomy_level = "{level}"\n'
        f'[user]\nname = "{name}"\n[brain]\nbackend = "{backend}"\n',
        encoding="utf-8",
    )
    return path


def run_start(tmp_path: Path, *argv: str, **kw: Any) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    kw.setdefault("platform", "darwin")
    kw.setdefault("keys", lambda provider: lambda: f"sk-{provider}")
    kw.setdefault("preflight", lambda stt: None)
    code = commands.run_start(parse("start", *argv), out=out, err=err, **kw)
    return code, out.getvalue(), err.getvalue()


def meetings(tmp_path: Path) -> list[Path]:
    root = tmp_path / "meetings"
    return sorted(root.iterdir()) if root.exists() else []


# --- start -----------------------------------------------------------------------------


def test_start_is_macos_only(tmp_path: Path) -> None:
    code, _, err = run_start(tmp_path, "Daily", platform="linux")
    assert code == 2 and "macOS only" in err and "sombra replay" in err


@pytest.mark.parametrize(
    ("config", "argv", "keys", "message"),
    [
        ({"level": "L2"}, ["--level", "L3"], None, "invalid choice"),
        ({"name": ""}, [], None, "[user] name"),
        ({}, [], "none", "sombra auth set anthropic"),
    ],
)
def test_start_refuses_before_creating_anything(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    config: dict[str, str],
    argv: list[str],
    keys: str | None,
    message: str,
) -> None:
    cfg = write_config(tmp_path, **config)
    kw: dict[str, Any] = {"keys": (lambda provider: None)} if keys == "none" else {}
    try:
        code, _, err = run_start(tmp_path, "Daily", "--config", str(cfg), *argv, **kw)
    except SystemExit as e:  # argparse rejects L3 itself
        code, err = int(str(e.code)), capsys.readouterr().err
    assert code == 2 and message in err
    assert meetings(tmp_path) == []


def test_start_needs_the_models_downloaded(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # empty model cache
    cfg = write_config(tmp_path)
    code, _, err = run_start(tmp_path, "Daily", "--config", str(cfg), preflight=None)
    assert code == 2 and "download_models.py large-v3-turbo-q5_0" in err
    assert meetings(tmp_path) == []


def test_start_rejects_a_bad_config_or_profile(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("autonomy_level = 3\n")
    assert run_start(tmp_path, "Daily", "--config", str(bad))[0] == 2
    code, _, err = run_start(
        tmp_path, "Daily", "--config", str(write_config(tmp_path)), "--profile", "nope"
    )
    assert code == 2 and "nope" in err


def test_start_consent_refused_removes_the_new_meeting(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    ran: list[Any] = []
    code, _, err = run_start(
        tmp_path,
        "Daily",
        "--config",
        str(cfg),
        confirm=lambda notice: False,
        runner=lambda plan, args: ran.append(plan),
    )
    assert code == 1 and "not starting" in err
    assert meetings(tmp_path) == [] and ran == []


def test_start_runs_the_meeting_then_prints_the_report(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    seen: list[Any] = []
    notices: list[str] = []

    def confirm(notice: str) -> bool:
        notices.append(notice)
        return True

    code, out, _ = run_start(
        tmp_path,
        "Daily",
        "--config",
        str(cfg),
        "--level",
        "L1",
        "--window",
        "Zoom",
        confirm=confirm,
        runner=lambda plan, args: seen.append(plan),
    )
    assert code == 0
    (meeting,) = meetings(tmp_path)
    (plan,) = seen
    assert plan.meeting_dir == meeting and plan.level is AutonomyLevel.L1
    assert plan.window == "Zoom"
    assert plan.agent_key() == "sk-anthropic"
    assert isinstance(plan.summary, AnthropicTextModel)
    assert plan.summary.name == "claude-haiku-4-5"
    assert (meeting / "consent.json").is_file() and "Sombra" in notices[0]
    assert 'autonomy_level = "L1"' in (meeting / "meeting.toml").read_text()
    assert out.startswith(f"{meeting}\n")
    assert "Latency" in out or "latency" in out.lower()


def test_start_level_zero_runs_without_a_key(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, name="", level="L0")
    seen: list[Any] = []
    code, _, err = run_start(
        tmp_path,
        "Gravação",
        "--config",
        str(cfg),
        keys=lambda provider: None,
        confirm=lambda _: True,
        runner=lambda plan, args: seen.append(plan),
    )
    assert code == 0 and "no summaries or minutes" in err
    assert seen[0].summary is None and seen[0].level is AutonomyLevel.L0


def test_start_with_codex_needs_no_anthropic_key(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, backend="codex")
    seen: list[Any] = []
    keys = {"openai": lambda: "sk-openai"}
    code, _, err = run_start(
        tmp_path,
        "Daily",
        "--config",
        str(cfg),
        keys=keys.get,
        confirm=lambda _: True,
        runner=lambda plan, args: seen.append(plan),
    )
    assert code == 0 and "summaries" not in err
    assert seen[0].agent_key() == "sk-openai"
    assert isinstance(seen[0].summary, CodexCliTextModel)  # summaries follow the agent


@pytest.mark.parametrize(
    ("brain", "keys", "agent_key", "summary"),
    [
        # a subscription never gets a key, even when one is stored
        ('backend = "claude-code"', {"anthropic": "sk-a"}, None, ClaudeCliTextModel),
        ('backend = "codex"\nauth = "subscription"', {"openai": "sk-o"}, None, CodexCliTextModel),
        ('backend = "codex"\nauth = "api-key"', {"openai": "sk-o"}, "sk-o", CodexCliTextModel),
        ('backend = "claude"', {"anthropic": "sk-a"}, "sk-a", AnthropicTextModel),
    ],
)
def test_start_builds_the_configured_provider(
    tmp_path: Path, brain: str, keys: dict[str, str], agent_key: str | None, summary: type
) -> None:
    cfg = write_config(tmp_path)
    cfg.write_text(cfg.read_text().replace('backend = "claude"', brain))
    seen: list[Any] = []
    lookup = {k: (lambda v=v: v) for k, v in keys.items()}
    code, _, err = run_start(
        tmp_path,
        "Daily",
        "--config",
        str(cfg),
        keys=lookup.get,
        confirm=lambda _: True,
        runner=lambda plan, args: seen.append(plan),
    )
    assert code == 0, err
    plan = seen[0]
    assert (plan.agent_key() if plan.agent_key else None) == agent_key
    assert isinstance(plan.summary, summary)


def test_start_codex_api_key_mode_needs_the_key(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    cfg.write_text(
        cfg.read_text().replace('backend = "claude"', 'backend = "codex"\nauth = "api-key"')
    )
    code, _, err = run_start(tmp_path, "Daily", "--config", str(cfg), keys=lambda p: None)
    assert code == 2 and "sombra auth set openai" in err
    assert meetings(tmp_path) == []


def test_start_summary_none(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    cfg.write_text(cfg.read_text() + '[summary]\nbackend = "none"\n')
    seen: list[Any] = []
    code, _, err = run_start(
        tmp_path,
        "Daily",
        "--config",
        str(cfg),
        confirm=lambda _: True,
        runner=lambda plan, args: seen.append(plan),
    )
    assert code == 0 and "no rolling summaries or minutes" in err
    assert seen[0].summary is None


# --- replay ----------------------------------------------------------------------------


def run_replay(tmp_path: Path, *argv: str, **kw: Any) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    cfg = write_config(tmp_path)
    args = parse("replay", *argv, "--config", str(cfg), "--root", str(tmp_path / "meetings"))
    code = commands.run_replay_command(args, out=out, err=err, **kw)
    return code, out.getvalue(), err.getvalue()


ME, OTHERS, FRAMES = str(FIXTURE / "me.wav"), str(FIXTURE / "others.wav"), str(FIXTURE / "frames")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["-", "-"], "at least one WAV"),
        ([str(FIXTURE / "missing.wav"), "-"], "missing.wav"),
        ([FRAMES + "/1790703000000.json", "-"], "sombra replay:"),
        ([ME, OTHERS, "--frames", str(FIXTURE / "nope")], "not a folder"),
        ([ME, OTHERS, "--speed", "0"], "--speed"),
    ],
)
def test_replay_rejects_bad_inputs(tmp_path: Path, argv: list[str], message: str) -> None:
    code, _, err = run_replay(tmp_path, *argv)
    assert code == 2 and message in err


def test_replay_needs_a_key_unless_the_brain_is_fake(tmp_path: Path) -> None:
    code, _, err = run_replay(tmp_path, ME, OTHERS, keys=lambda provider: None)
    assert code == 2 and "--fake-brain" in err


def test_replay_needs_a_user_name(tmp_path: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    cfg = write_config(tmp_path, name="")
    args = parse("replay", ME, OTHERS, "--config", str(cfg), "--fake-brain", "--auto-approve")
    assert commands.run_replay_command(args, out=out, err=err) == 2
    assert "--user" in err.getvalue()


def test_replay_reports_a_missing_whisper_model(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # empty model cache
    code, _, err = run_replay(
        tmp_path, ME, OTHERS, "--fake-brain", "--auto-approve", "--stt-model", "tiny"
    )
    assert code == 1 and "download_models" in err
    assert meetings(tmp_path) == []  # checked before any folder is made


def test_replay_cli_end_to_end_with_fake_models(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "sombra.orchestrator.replay.build_transcriber", lambda *a, **k: fake_transcriber()
    )
    code, out, err = run_replay(
        tmp_path,
        ME,
        OTHERS,
        "--frames",
        FRAMES,
        "--fake-brain",
        "--auto-approve",
        "--name",
        "Vendas",
        "--user",
        "Maria",
    )
    assert code == 0, err
    meeting = Path(out.splitlines()[0])
    assert meeting.name.endswith("_vendas")
    events = [json.loads(x) for x in (meeting / "log.jsonl").read_text().splitlines()]
    assert [e["type"] for e in events] == ["trigger", "suggestion", "action"]
    assert events[1]["frames_sent"] == ["f0001"]
    assert not (meeting / "summary.md").exists()  # fake brain: no API, no minutes


@pytest.mark.parametrize(
    ("brain", "agent_key", "summary"),
    [
        ('backend = "claude-code"', None, ClaudeCliTextModel),
        ('backend = "codex"\nauth = "subscription"', None, CodexCliTextModel),
        ('backend = "codex"\nauth = "api-key"', "sk-openai", CodexCliTextModel),
        ('backend = "claude-api"', "sk-anthropic", AnthropicTextModel),
    ],
)
def test_replay_builds_the_configured_provider(
    tmp_path: Path, monkeypatch: Any, brain: str, agent_key: str | None, summary: type
) -> None:
    from sombra.orchestrator.replay import ScriptedBrain

    monkeypatch.setattr(
        "sombra.orchestrator.replay.build_transcriber", lambda *a, **k: fake_transcriber()
    )
    built: list[tuple[str, str | None]] = []

    def fake_agent_brain(backend: str, api_key: Any, **kw: Any) -> ScriptedBrain:
        built.append((backend, api_key() if api_key else None))
        return ScriptedBrain()

    summaries: list[Any] = []
    real_run_replay = commands_replay.run_replay

    async def spy(opts: Any, **kw: Any) -> Path:
        summaries.append(kw["summary_model"])
        return await real_run_replay(opts, **{**kw, "summary_model": None})

    monkeypatch.setattr("sombra.orchestrator.replay.build_agent_brain", fake_agent_brain)
    monkeypatch.setattr(commands_replay, "run_replay", spy)
    cfg = write_config(tmp_path)
    cfg.write_text(cfg.read_text().replace('backend = "claude"', brain))
    out, err = io.StringIO(), io.StringIO()
    args = parse(
        "replay", ME, OTHERS, "--auto-approve", "--config", str(cfg), "--root", str(tmp_path / "m")
    )
    keys = lambda provider: lambda: f"sk-{provider}"  # noqa: E731
    code = commands.run_replay_command(args, out=out, err=err, keys=keys)
    assert code == 0, err.getvalue()
    backend = brain.split('"')[1]
    assert built == [(backend, agent_key)]
    assert isinstance(summaries[0], summary)
