"""``sombra ask``: meeting resolution, the post-meeting prompt variant and printed output.

The brain is faked two ways: ``FakeBrain`` (the contract level, for the command) and
a scripted ``ModelClient`` under a real ``ClaudeBrain`` (to see the exact request the
post-meeting variant sends).
"""

from __future__ import annotations

import argparse
import io
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ask_fixture import STARTED, make_meeting

from fakes import FakeBrain
from sombra.brain.claude import ModelReply
from sombra.brain.prompt import (
    ASK_NOT_FOUND,
    build_ask_tail,
    post_meeting_system_prompt,
    system_prompt,
)
from sombra.cli import build_parser
from sombra.config import UserConfig, UserIdentity
from sombra.contracts import AutonomyLevel, Brain, BrainRequest, BrainResponse, Usage
from sombra.orchestrator import ask as ask_mod
from sombra.orchestrator import commands
from sombra.orchestrator.ask import (
    FRAMES_OFF_NOTE,
    ApiKeyMissingError,
    FrameGate,
    MeetingNotFoundError,
    ask_trigger,
    claude_brain,
    env_api_key,
    list_meetings,
    read_summary,
    resolve_meeting,
    resolve_model,
)

# --- helpers ---------------------------------------------------------------------------


def _config(root: Path, tmp_path: Path, name: str = "Nick", agent: str = "sonnet") -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'meetings_root = "{root}"\n[user]\nname = "{name}"\naliases = ["Nico"]\n'
        f'[models]\nagent = "{agent}"\n',
        encoding="utf-8",
    )
    return cfg


def _args(meeting: str, question: str, config: Path, **kw: Any) -> argparse.Namespace:
    parsed = build_parser().parse_args(["ask", meeting, question, "--config", str(config)])
    for key, value in kw.items():
        setattr(parsed, key, value)
    return parsed


class Script:
    """Scripted ``ModelClient``: returns ``replies`` in order and records every request."""

    def __init__(self, *replies: ModelReply) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    async def create(self, request: dict[str, Any], *, timeout_s: float) -> ModelReply:
        self.requests.append(request)
        return self.replies.pop(0)

    async def close(self) -> None:
        self.closed = True


def _text_reply(text: str) -> ModelReply:
    return ModelReply(
        content=[{"type": "text", "text": text}],
        stop_reason="end_turn",
        usage=Usage(input_tokens=10, output_tokens=5, cache_read_input_tokens=100),
        model="claude-sonnet-5-5",
    )


def _tool_reply(name: str, args: dict[str, Any]) -> ModelReply:
    return ModelReply(
        content=[{"type": "tool_use", "id": "t1", "name": name, "input": args}],
        stop_reason="tool_use",
    )


def _all_text(request: dict[str, Any]) -> str:
    parts = [b.get("text", "") for b in request["system"]]
    for message in request["messages"]:
        for block in message["content"]:
            parts.append(str(block.get("text", "")))
            for inner in block.get("content", []) if isinstance(block, dict) else []:
                if isinstance(inner, dict):
                    parts.append(str(inner.get("text", "")))
    return "\n".join(parts)


def _types(request: dict[str, Any]) -> list[str]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "type" in node:
                found.append(str(node["type"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(request["messages"])
    return found


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "meetings"


@pytest.fixture
def no_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield


# --- meeting resolution ----------------------------------------------------------------


def test_latest_is_the_most_recently_started_meeting(root: Path) -> None:
    older = make_meeting(root, "Zeta review", started=STARTED - timedelta(days=1))
    newer = make_meeting(root, "Alpha planning", started=STARTED)
    # Name order would pick "zeta"; started_at picks the newer meeting.
    assert resolve_meeting("latest", root) == newer.resolve()
    assert list_meetings(root) == [older, newer]


def test_latest_with_no_meetings_fails(root: Path) -> None:
    with pytest.raises(MeetingNotFoundError, match="no meetings"):
        resolve_meeting("latest", root)
    root.mkdir()
    (root / "not-a-meeting").mkdir()
    with pytest.raises(MeetingNotFoundError):
        resolve_meeting("latest", root)


def test_latest_tolerates_a_broken_meeting_toml(root: Path) -> None:
    good = make_meeting(root, "Daily")
    broken = root / "0000-broken"
    broken.mkdir()
    (broken / "meeting.toml").write_text("started_at = 'not a date'\n", encoding="utf-8")
    assert list_meetings(root)[0] == broken  # unreadable ones sort first
    assert resolve_meeting("latest", root) == good.resolve()


def test_resolves_a_path(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    meeting = make_meeting(tmp_path / "elsewhere", "Daily")
    assert resolve_meeting(str(meeting), root) == meeting.resolve()
    monkeypatch.chdir(meeting.parent)
    assert resolve_meeting(meeting.name, root) == meeting.resolve()  # relative path


def test_resolves_a_folder_name_under_the_root(root: Path) -> None:
    meeting = make_meeting(root, "Daily time X")
    assert resolve_meeting("2026-09-29_1430_daily-time-x", root) == meeting.resolve()


def test_resolves_a_name_or_slug_to_its_latest_occurrence(root: Path) -> None:
    make_meeting(root, "Daily time X", started=STARTED - timedelta(days=1))
    make_meeting(root, "Planning", started=STARTED + timedelta(hours=2))
    today = make_meeting(root, "Daily time X", started=STARTED)
    assert resolve_meeting("Daily time X", root) == today.resolve()
    assert resolve_meeting("daily-time-x", root) == today.resolve()
    assert resolve_meeting("  DAILY TIME X ", root) == today.resolve()


def test_name_matches_a_deduplicated_folder(root: Path) -> None:
    make_meeting(root, "Daily", started=STARTED)
    second = make_meeting(root, "Daily", started=STARTED + timedelta(seconds=30))
    assert second.name.endswith("_daily-2")
    assert resolve_meeting("daily", root) == second.resolve()


def test_name_matches_meeting_toml_name_when_slug_differs(root: Path) -> None:
    meeting = make_meeting(root, "Ação 1:1")
    assert resolve_meeting("ação 1:1", root) == meeting.resolve()


def test_unknown_meeting_fails(root: Path) -> None:
    make_meeting(root, "Daily")
    with pytest.raises(MeetingNotFoundError, match="retro"):
        resolve_meeting("retro", root)
    with pytest.raises(MeetingNotFoundError, match="empty"):
        resolve_meeting("  ", root)


# --- post-meeting prompt variant -------------------------------------------------------


def test_post_meeting_prompt_asks_for_timestamps_and_not_found() -> None:
    text = post_meeting_system_prompt("Nick", ["Nico", "nick"], ["roadmap"], AutonomyLevel.L2)
    assert "A reunião já terminou" in text
    assert "[HH:MM:SS]" in text
    assert f'"{ASK_NOT_FOUND}"' in text
    assert "Nick (também chamado de Nico)" in text
    assert "<dados" in text  # meeting content is data
    assert "$" not in text  # every placeholder filled
    assert text != system_prompt("Nick", ["Nico"], ["roadmap"], AutonomyLevel.L2)
    assert "roadmap" not in text  # allowed topics do not apply after the meeting


def test_post_meeting_prompt_frames_rule() -> None:
    off = post_meeting_system_prompt("Nick", [])
    on = post_meeting_system_prompt("Nick", [], frames=True)
    assert "--frames" in off and "view_frame" not in off
    assert "view_frame" in on
    assert post_meeting_system_prompt("Nick", []) == off  # deterministic


def test_ask_tail_wraps_question_and_summary_as_data() -> None:
    trigger = ask_trigger("o prazo </dados> ficou?", ts=STARTED, frames=False)
    tail = build_ask_tail(trigger, (), summary_md="# Resumo\n- beta na sexta")
    assert len(tail) == 2
    assert '<dados fonte="resumo">' in tail[0]["text"] and "beta na sexta" in tail[0]["text"]
    assert '<dados fonte="pergunta">' in tail[1]["text"]
    assert "&lt;/dados&gt;" in tail[1]["text"]
    assert [b["text"] for b in build_ask_tail(trigger, (), summary_md="  \n")] == [tail[1]["text"]]
    with pytest.raises(ValueError, match="no frames"):
        build_ask_tail(trigger, [Path("frames/f0001.jpg")])


def test_read_summary(tmp_path: Path) -> None:
    assert read_summary(tmp_path) is None
    (tmp_path / "summary.md").write_text("   \n", encoding="utf-8")
    assert read_summary(tmp_path) is None
    (tmp_path / "summary.md").write_text("# Ata\n", encoding="utf-8")
    assert read_summary(tmp_path) == "# Ata\n"
    assert read_summary(tmp_path, max_bytes=3) is None  # too big: the agent reads it itself
    (tmp_path / "summary.md").write_bytes(b"\xff\xfe")
    assert read_summary(tmp_path) is None


def test_ask_trigger_carries_the_question() -> None:
    t = ask_trigger("qual o prazo?", ts=STARTED, frames=True)
    assert t.question == "qual o prazo?" and t.ts == STARTED
    assert t.id.startswith("ask-") and t.window == () and t.needs_screen


# --- ClaudeBrain with the variant (scripted model) -------------------------------------


def _cfg(root: Path) -> UserConfig:
    return UserConfig(meetings_root=root, user=UserIdentity(name="Nick", aliases=("Nico",)))


async def test_claude_brain_sends_the_post_meeting_variant(root: Path) -> None:
    meeting = make_meeting(root, "Daily", summary="# Resumo\n- beta sexta\n")
    script = Script(_text_reply("O beta sai na sexta [14:31:02]."))
    brain = claude_brain(meeting, _cfg(root), frames=False, client=script)
    result = await ask_mod.ask(brain, meeting, "Qual o prazo do beta?", frames=False, clock=_now)

    assert result.response.text == "O beta sai na sexta [14:31:02]."
    assert result.response.model == "claude-sonnet-5-5"
    assert script.closed
    [request] = script.requests  # no warm-up call for a single question
    system = request["system"][0]["text"]
    assert system == post_meeting_system_prompt("Nick", ["Nico"])
    assert request["model"] == "claude-sonnet-5-5"  # "sonnet" from the config
    body = _all_text(request)
    assert "[14:31:02] EU: combinamos que o beta sai na sexta" in body  # transcript prefix
    assert "beta sexta" in body  # summary.md in the tail
    assert "Qual o prazo do beta?" in body
    assert "view_frame" not in {t["name"] for t in request["tools"]}
    assert "image" not in _types(request)


async def test_without_frames_no_image_leaves_the_machine(root: Path) -> None:
    meeting = make_meeting(root, "Daily")
    script = Script(
        _tool_reply("read", {"path": "frames/f0001.jpg"}),
        _text_reply("Não consigo ver a tela sem --frames."),
    )
    brain = claude_brain(meeting, _cfg(root), frames=False, client=script)
    result = await ask_mod.ask(brain, meeting, "O que tinha na tela?", frames=False, clock=_now)

    second = script.requests[1]
    assert "image" not in _types(second)
    assert FRAMES_OFF_NOTE in _all_text(second)
    assert result.response.frames_sent == []


async def test_with_frames_the_agent_can_view_a_frame(root: Path) -> None:
    meeting = make_meeting(root, "Daily")
    script = Script(
        _tool_reply("view_frame", {"frame_id": "f0001"}),
        _text_reply("O dashboard de churn [14:32:10] (TELA f0001)."),
    )
    brain = claude_brain(meeting, _cfg(root), frames=True, client=script, model="opus")
    result = await ask_mod.ask(brain, meeting, "O que tinha na tela?", frames=True, clock=_now)

    first, second = script.requests
    assert first["model"] == "claude-opus-5-5"
    assert "view_frame" in {t["name"] for t in first["tools"]}
    assert first["system"][0]["text"] == post_meeting_system_prompt("Nick", ["Nico"], frames=True)
    assert "image" in _types(second)
    assert result.response.frames_sent == ["f0001"]


async def test_frame_gate_passes_through_with_frames() -> None:
    script = Script(_text_reply("ok"))
    gate = FrameGate(script, frames=True)
    request = {"tools": [{"name": "view_frame"}], "messages": [{"role": "user", "content": "x"}]}
    await gate.create(request, timeout_s=1)
    assert script.requests[0] is request
    await gate.close()
    assert script.closed


async def test_frame_gate_strips_nested_images_only() -> None:
    script = Script(_text_reply("ok"))
    image = {"type": "image", "source": {"type": "base64", "data": "x"}}
    request = {
        "messages": [
            {"role": "user", "content": "plain string"},
            {
                "role": "user",
                "content": [
                    image,
                    "odd",
                    {"type": "tool_result", "tool_use_id": "t", "content": [image]},
                    {"type": "text", "text": "keep"},
                ],
            },
        ]
    }
    await FrameGate(script, frames=False).create(request, timeout_s=1)
    sent = script.requests[0]
    assert "tools" not in sent
    assert sent["messages"][0]["content"] == "plain string"
    content = sent["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": FRAMES_OFF_NOTE}
    assert content[1] == "odd"
    assert content[2]["content"] == [{"type": "text", "text": FRAMES_OFF_NOTE}]
    assert content[3] == {"type": "text", "text": "keep"}
    assert request["messages"][1]["content"][0] is image  # the brain's copy is untouched


def test_resolve_model() -> None:
    assert resolve_model("sonnet") == "claude-sonnet-5-5"
    assert resolve_model(" Haiku ") == "claude-haiku-4-5"
    assert resolve_model("claude-opus-5-5") == "claude-opus-5-5"


def test_env_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", " sk-test ")
    assert env_api_key() == "sk-test"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(ApiKeyMissingError, match="ANTHROPIC_API_KEY"):
        env_api_key()


async def test_ask_rejects_an_empty_question(tmp_path: Path) -> None:
    brain = FakeBrain()
    with pytest.raises(ValueError, match="empty"):
        await ask_mod.ask(brain, tmp_path, "  ", frames=False, clock=_now)
    assert not brain.started


def _now() -> datetime:
    return STARTED + timedelta(hours=3)


# --- the command, with a fake brain ----------------------------------------------------


def _fake_factory(
    brain: Brain, seen: list[tuple[Path, UserConfig, argparse.Namespace]]
) -> commands.BrainFactory:
    def make(meeting_dir: Path, cfg: UserConfig, args: argparse.Namespace) -> Brain:
        seen.append((meeting_dir, cfg, args))
        return brain

    return make


@pytest.mark.parametrize("ref", ["latest", "Daily time X", "daily-time-x", "PATH"])
def test_command_resolves_meeting_and_prints_answer(root: Path, tmp_path: Path, ref: str) -> None:
    meeting = make_meeting(root, "Daily time X")
    make_meeting(root, "Older", started=STARTED - timedelta(days=2))
    brain = FakeBrain()
    seen: list[tuple[Path, UserConfig, argparse.Namespace]] = []
    out, err = io.StringIO(), io.StringIO()
    args = _args(str(meeting) if ref == "PATH" else ref, "Qual o prazo?", _config(root, tmp_path))

    code = commands.run(args, make_brain=_fake_factory(brain, seen), out=out, err=err)

    assert code == 0
    assert out.getvalue() == "resposta 1\n"
    assert brain.meeting_dir == meeting.resolve() and brain.closed
    [request] = brain.requests
    assert request.trigger.question == "Qual o prazo?" and request.frame_paths == ()
    assert seen[0][1].user.name == "Nick"
    assert f"reunião: {meeting.resolve()}" in err.getvalue()
    assert "modelo: fake-model" in err.getvalue()
    assert "tokens: 100 in, 20 out" in err.getvalue()
    assert "telas" not in err.getvalue()


class _FramesBrain(FakeBrain):
    async def answer(self, request: BrainRequest) -> BrainResponse:
        await super().answer(request)
        return BrainResponse("veja a tela", ["f0001"], "fake", "fake-model")


def test_command_frames_flag(root: Path, tmp_path: Path) -> None:
    make_meeting(root, "Daily")
    config = _config(root, tmp_path)
    for frames, expected in ((True, "telas: f0001"), (False, None)):
        brain = _FramesBrain()
        seen: list[tuple[Path, UserConfig, argparse.Namespace]] = []
        err = io.StringIO()
        args = _args("latest", "e a tela?", config, frames=frames)
        assert (
            commands.run(args, make_brain=_fake_factory(brain, seen), out=io.StringIO(), err=err)
            == 0
        )
        assert seen[0][2].frames is frames
        assert brain.requests[0].trigger.needs_screen is frames
        if expected:
            assert expected in err.getvalue()
        else:
            assert "telas" not in err.getvalue()  # frames off: never reported as sent


def test_command_unknown_meeting_exits_2(root: Path, tmp_path: Path) -> None:
    make_meeting(root, "Daily")
    err = io.StringIO()
    brain = FakeBrain()
    args = _args("retro", "q?", _config(root, tmp_path))
    assert commands.run(args, make_brain=_fake_factory(brain, []), err=err) == 2
    assert "no meeting 'retro'" in err.getvalue()
    assert not brain.started


def test_command_empty_question_exits_2(root: Path, tmp_path: Path) -> None:
    make_meeting(root, "Daily")
    err = io.StringIO()
    args = _args("latest", "   ", _config(root, tmp_path))
    assert commands.run(args, make_brain=_fake_factory(FakeBrain(), []), err=err) == 2
    assert "question is empty" in err.getvalue()


def test_command_bad_config_exits_2(root: Path, tmp_path: Path) -> None:
    cfg = tmp_path / "bad.toml"
    cfg.write_text("autonomy_level = 'L9'\n", encoding="utf-8")
    err = io.StringIO()
    assert commands.run(_args("latest", "q?", cfg), err=err) == 2
    assert "autonomy_level" in err.getvalue()


def test_command_brain_failure_exits_1(root: Path, tmp_path: Path) -> None:
    make_meeting(root, "Daily")
    brain = FakeBrain(fail_on={0})
    err, out = io.StringIO(), io.StringIO()
    args = _args("latest", "q?", _config(root, tmp_path))
    assert commands.run(args, make_brain=_fake_factory(brain, []), out=out, err=err) == 1
    assert "o agente falhou (RuntimeError)" in err.getvalue()
    assert out.getvalue() == ""
    assert brain.closed  # closed even when the answer fails


def test_command_without_api_key_exits_2(root: Path, tmp_path: Path, no_key: None) -> None:
    make_meeting(root, "Daily")
    err = io.StringIO()
    assert commands.run(_args("latest", "q?", _config(root, tmp_path)), err=err) == 2
    assert "ANTHROPIC_API_KEY" in err.getvalue()


def test_command_writes_nothing_to_the_meeting(root: Path, tmp_path: Path) -> None:
    meeting = make_meeting(root, "Daily")
    before = {p: p.read_bytes() for p in meeting.rglob("*") if p.is_file()}
    args = _args("latest", "q?", _config(root, tmp_path))
    assert (
        commands.run(
            args, make_brain=_fake_factory(FakeBrain(), []), out=io.StringIO(), err=io.StringIO()
        )
        == 0
    )
    after = {p: p.read_bytes() for p in meeting.rglob("*") if p.is_file()}
    assert after == before


def test_parser_registers_ask() -> None:
    args = build_parser().parse_args(["ask", "latest", "o prazo?", "--frames", "--model", "opus"])
    assert args.func is commands._ask
    assert (args.meeting, args.question, args.frames, args.model) == (
        "latest",
        "o prazo?",
        True,
        "opus",
    )
    assert build_parser().parse_args(["ask", "latest", "q"]).frames is False


def test_default_entry_point_uses_run(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[argparse.Namespace] = []
    monkeypatch.setattr(commands, "run", lambda a: called.append(a) or 7)
    ns = argparse.Namespace()
    assert commands._ask(ns) == 7 and called == [ns]


def test_claude_brain_uses_the_real_client_with_a_key(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    meeting = make_meeting(root, "Daily")
    brain = claude_brain(meeting, _cfg(root), frames=False)  # builds only; no request is made
    assert brain.settings.model == "claude-sonnet-5-5"
    assert brain.settings.warm_on_start is False and brain.settings.cache_ttl == "5m"
