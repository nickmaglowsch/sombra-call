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


def parse(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(list(argv))


def write_config(tmp_path: Path, name: str = "Mariana", level: str = "L2") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'meetings_root = "{tmp_path / "meetings"}"\nautonomy_level = "{level}"\n'
        f'[user]\nname = "{name}"\n',
        encoding="utf-8",
    )
    return path


def run_start(tmp_path: Path, *argv: str, **kw: Any) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    kw.setdefault("platform", "darwin")
    kw.setdefault("keys", lambda: lambda: "sk-test")
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
    kw: dict[str, Any] = {"keys": (lambda: None)} if keys == "none" else {}
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
    assert plan.window == "Zoom" and plan.api_key() == "sk-test"
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
        keys=lambda: None,
        confirm=lambda _: True,
        runner=lambda plan, args: seen.append(plan),
    )
    assert code == 0 and "no minutes" in err
    assert seen[0].api_key is None and seen[0].level is AutonomyLevel.L0


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
    code, _, err = run_replay(tmp_path, ME, OTHERS, keys=lambda: None)
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
        "Mariana",
    )
    assert code == 0, err
    meeting = Path(out.splitlines()[0])
    assert meeting.name.endswith("_vendas")
    events = [json.loads(x) for x in (meeting / "log.jsonl").read_text().splitlines()]
    assert [e["type"] for e in events] == ["trigger", "suggestion", "action"]
    assert events[1]["frames_sent"] == ["f0001"]
    assert not (meeting / "summary.md").exists()  # fake brain: no API, no minutes
