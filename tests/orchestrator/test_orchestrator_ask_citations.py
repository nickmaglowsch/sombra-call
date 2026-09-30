"""``sombra ask`` prints only ``[HH:MM:SS]`` citations that exist in ``transcript.md`` (#77).

The fake brains answer with the invented ``[00:00:00]`` the Claude Code backend cited on
the Mac, alone and mixed with real times. The fixture transcript (``ask_fixture``) has
lines at 14:30:05, 14:30:40, 14:31:02, 14:31:20, 14:32:10 (TELA), 14:32:15, 14:33:00,
14:33:30 and 14:34:00.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from ask_fixture import STARTED, make_meeting

from fakes import FakeBrain
from sombra.brain.codex import ProcessResult
from sombra.cli import build_parser
from sombra.config import UserConfig
from sombra.contracts import BrainRequest, BrainResponse
from sombra.orchestrator import ask as ask_mod
from sombra.orchestrator import commands
from sombra.orchestrator.ask import FramelessView, ask, ask_prompt_kit, claude_code_brain
from sombra.orchestrator.citations import (
    CheckedAnswer,
    check_citations,
    clock_hint,
    transcript_times,
)

KNOWN = {"14:30:05", "14:30:40", "14:31:02", "14:31:20", "14:32:10", "14:33:00"}


# --- the rule ------------------------------------------------------------------------


def test_valid_citations_are_left_byte_for_byte() -> None:
    text = "O beta sai na sexta [14:31:02], confirmado [14:31:20].\n- Ana [ 14:33:00 ]"
    assert check_citations(text, KNOWN).text == text
    assert check_citations(text, KNOWN).dropped == ()


def test_invented_citation_is_dropped_with_its_space() -> None:
    checked = check_citations("Isso não está na transcrição [00:00:00].", KNOWN)
    assert checked.text == "Isso não está na transcrição."
    assert checked.dropped == ("00:00:00",)


def test_invented_citation_at_line_start_takes_the_space_after() -> None:
    checked = check_citations("[00:00:00] O prazo é sexta [14:31:02].", KNOWN)
    assert checked.text == "O prazo é sexta [14:31:02]."


def test_invented_citation_mid_sentence_keeps_one_space() -> None:
    checked = check_citations("falou [00:00:00] e depois [09:99:00] saiu", KNOWN)
    assert checked.text == "falou e depois saiu"
    assert checked.dropped == ("00:00:00", "09:99:00")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sexta [14:31:02, 00:00:00].", "sexta [14:31:02]."),
        ("sexta [00:00:00 e 14:31:20].", "sexta [14:31:20]."),
        ("sexta [14:30:05–14:31:02].", "sexta [14:30:05–14:31:02]."),
        ("sexta [14:30:05 – 23:59:59].", "sexta [14:30:05]."),
        ("sexta [00:00:00-14:33:00].", "sexta [14:33:00]."),  # a range keeps its real end
        ("sexta [14:30:05, 00:00:00, 14:31:02].", "sexta [14:30:05, 14:31:02]."),
    ],
)
def test_multi_time_brackets_keep_only_real_times(text: str, expected: str) -> None:
    assert check_citations(text, KNOWN).text == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # --frames asks the model to cite the screen id next to the time
        ("a tela [00:00:00, f0003] b", "a tela [f0003] b"),
        ("a tela [00:00:00 f0003] b", "a tela [f0003] b"),
        ("a tela [00:00:00 TELA f0003] b", "a tela [TELA f0003] b"),
        ("a tela [00:00:00 – TELA] b", "a tela b"),
        ("a tela [14:31:02, 00:00:00, f0003] b", "a tela [14:31:02, f0003] b"),
    ],
)
def test_brackets_with_a_frame_id_are_citations_too(text: str, expected: str) -> None:
    checked = check_citations(text, KNOWN)
    assert checked.text == expected
    assert checked.dropped == ("00:00:00",)


@pytest.mark.parametrize(
    "text", ["a tela [14:32:10, f0001] b", "a tela [14:32:10 f0001] b", "[14:32:10 – TELA]"]
)
def test_valid_brackets_with_a_frame_id_are_unchanged(text: str) -> None:
    assert check_citations(text, KNOWN) == CheckedAnswer(text, ())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Não está **[00:00:00]**.", "Não está."),
        ("Não está _[00:00:00]_ aqui.", "Não está aqui."),
        ("Beta **[00:00:00, 14:31:02]**.", "Beta **[14:31:02]**."),
        ("Beta **[14:31:02]**.", "Beta **[14:31:02]**."),
    ],
)
def test_emphasis_around_a_dropped_citation_goes_too(text: str, expected: str) -> None:
    assert check_citations(text, KNOWN).text == expected


def test_same_rule_as_the_minutes_normalizes_hours() -> None:
    # normalize_hms (#12): "9:05:03" is 09:05:03, a time that is not there is dropped
    assert check_citations("a [9:05:03] b", {"09:05:03"}).text == "a [9:05:03] b"
    assert check_citations("a [9:05:03] b", KNOWN).text == "a b"


def test_non_citations_are_left_alone() -> None:
    text = "Às 00:00:00 nada; veja [link](x) e [TELA f0001] e [nota]."
    assert check_citations(text, KNOWN).text == text


def test_link_text_with_an_invented_time_is_checked() -> None:
    assert check_citations("veja [00:00:00](x) e [14:31:02](y)", KNOWN).text == (
        "veja(x) e [14:31:02](y)"
    )


def test_empty_transcript_leaves_no_citation() -> None:
    assert check_citations("x [14:31:02].", set()).text == "x."


def test_transcript_times_reads_the_meeting(tmp_path: Path) -> None:
    meeting = make_meeting(tmp_path / "meetings")
    times = transcript_times(meeting, day=STARTED)
    assert times[0] == "14:30:05" and times[-1] == "14:34:00"
    assert "14:32:10" in times  # TELA lines count, as in the minutes
    assert transcript_times(tmp_path / "nowhere", day=STARTED) == []


def test_clock_hint_gives_the_real_range() -> None:
    hint = clock_hint(["14:30:05", "14:34:00"])
    assert hint is not None and "[14:30:05] a [14:34:00]" in hint and "00:00:00" in hint
    assert clock_hint([]) is None


def test_prompt_tail_carries_the_clock_hint(tmp_path: Path) -> None:
    meeting = make_meeting(tmp_path / "meetings")
    kit = ask_prompt_kit(meeting, frames=False)
    tail = kit.build_tail(ask_mod.ask_trigger("Qual o prazo?", ts=STARTED, frames=False), ())
    assert "Qual o prazo?" in tail[-2]["text"]
    assert "de [14:30:05] a [14:34:00]" in tail[-1]["text"]


def test_prompt_tail_without_a_transcript_has_no_hint(tmp_path: Path) -> None:
    meeting = make_meeting(tmp_path / "meetings")
    (meeting / "transcript.md").unlink()
    kit = ask_prompt_kit(meeting, frames=False)
    tail = kit.build_tail(ask_mod.ask_trigger("q?", ts=STARTED, frames=False), ())
    assert "horas do relógio" not in tail[-1]["text"]


# --- fake brains, through ask() and the command --------------------------------------


class _Says(FakeBrain):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    async def answer(self, request: BrainRequest) -> BrainResponse:
        await super().answer(request)
        return BrainResponse(self.text, [], "fake", "fake-model")


def _now() -> Any:
    return STARTED + timedelta(hours=3)


async def test_ask_result_has_no_invented_time(tmp_path: Path) -> None:
    meeting = make_meeting(tmp_path / "meetings")
    brain = _Says("Não está [00:00:00]. O mais próximo: beta na sexta [14:31:02].")
    result = await ask(brain, meeting, "q?", frames=False, clock=_now)
    assert result.response.text == "Não está. O mais próximo: beta na sexta [14:31:02]."
    assert result.dropped_citations == ("00:00:00",)


ANSWERS = [
    "Isso não está na transcrição [00:00:00].",
    "[00:00:00] O beta sai na sexta [14:31:02], a Ana cuida da migração [14:33:00] "
    "e o churn foi 2,4% [13:49:17] [14:32:15].",
]


@pytest.mark.parametrize("answer", ANSWERS)
def test_command_never_prints_an_invented_time(
    answer: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "meetings"
    make_meeting(root)
    config = tmp_path / "config.toml"
    config.write_text(f'meetings_root = "{root}"\n[user]\nname = "Nick"\n', encoding="utf-8")
    monkeypatch.setattr(ask_mod, "ask_brain", lambda *a, **kw: _Says(answer))
    args = build_parser().parse_args(["ask", "latest", "q?", "--config", str(config)])

    assert commands.run(args) == 0  # the real sys.stdout, captured

    out, err = capsys.readouterr()
    assert "00:00:00" not in out and "13:49:17" not in out
    for real in ("[14:31:02]", "[14:33:00]", "[14:32:15]"):
        assert (real in out) == (real in answer)  # valid ones printed unchanged
    assert "horários fora da transcrição removidos: 00:00:00" in err


# --- the Claude Code backend (scripted CLI) ------------------------------------------


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
        self.stdins: list[str] = []

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
        self.stdins.append(stdin)
        return ProcessResult(0, _stream(self.answer), "")


def test_claude_code_backend_answer_is_checked_before_printing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "meetings"
    make_meeting(root)
    config = tmp_path / "config.toml"
    config.write_text(
        f'meetings_root = "{root}"\n[user]\nname = "Nick"\n[brain]\nbackend = "claude-code"\n',
        encoding="utf-8",
    )
    cli = ScriptedClaude(
        "Isso não está na transcrição [00:00:00].\n\n"
        "Correção: os horários corretos são [14:31:02] e [14:31:20]."
    )

    def brain(meeting_dir: Path, cfg: UserConfig, **kw: Any) -> FramelessView:
        return FramelessView(claude_code_brain(meeting_dir, cfg, frames=False, runner=cli))

    monkeypatch.setattr(ask_mod, "ask_brain", brain)
    args = build_parser().parse_args(["ask", "latest", "q?", "--config", str(config)])

    assert commands.run(args) == 0

    out = capsys.readouterr().out
    assert out == (
        "Isso não está na transcrição.\n\n"
        "Correção: os horários corretos são [14:31:02] e [14:31:20].\n"
    )
    [stdin] = cli.stdins
    assert "de [14:30:05] a [14:34:00]" in stdin  # the model was given the real clock
