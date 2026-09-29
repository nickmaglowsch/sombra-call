import json
import logging
import re
from datetime import datetime
from pathlib import Path

import pytest
from summary_fakes import DAY, FakeModel, synthetic_lines, transcript_times

from sombra.summary import prompts
from sombra.summary.minutes import (
    ActionItem,
    Minutes,
    MinutesParseError,
    generate_minutes,
    normalize_hms,
    parse_minutes,
    render_minutes,
    validate_actions,
    write_minutes,
)
from sombra.summary.summary_file import (
    EpochSection,
    append_epoch,
    read_current_epoch,
    read_minutes_section,
)
from sombra.summary.transcript import chunk_lines, estimate_tokens, read_transcript

FIXTURE = Path(__file__).parent / "fixtures" / "daily_30min_ptbr.md"

GOOD = {
    "resumo": "Planejamento da sprint 42.",
    "decisoes": ["Integração com o ERP vai para o Q1"],
    "acoes": [
        {
            "descricao": "Estimar o painel de relatórios",
            "responsavel": "EU",
            "prazo": "sexta",
            "ref": "14:00:05",
        },
        {"descricao": "Inventada", "responsavel": None, "prazo": None, "ref": "23:59:59"},
    ],
    "perguntas_abertas": ["O backup cabe no disco novo?"],
}


def _meeting(tmp_path: Path, lines: list[str]) -> Path:
    (tmp_path / "transcript.md").write_text(
        "# Reunião sintética\n\n" + "\n".join(lines) + "\n", encoding="utf-8"
    )
    return tmp_path


# --- parsing ---------------------------------------------------------------------------


def test_parse_minutes_tolerates_fences_and_junk() -> None:
    text = "Claro! Aqui está:\n```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```\n"
    m = parse_minutes(text)
    assert m.summary == "Planejamento da sprint 42."
    assert m.decisions == ["Integração com o ERP vai para o Q1"]
    assert m.actions[0] == ActionItem("Estimar o painel de relatórios", "EU", "sexta", "14:00:05")
    assert m.actions[1].owner is None and m.actions[1].due is None
    assert m.open_questions == ["O backup cabe no disco novo?"]


def test_parse_minutes_skips_malformed_items() -> None:
    m = parse_minutes(
        json.dumps(
            {
                "resumo": None,
                "decisoes": "não é lista",
                "acoes": ["texto solto", {"descricao": ""}, {"descricao": "ok", "ref": 5}],
                "perguntas_abertas": ["", "  ", "real  com   espaços"],
            }
        )
    )
    assert m.summary == ""
    assert m.decisions == []
    assert m.actions == [ActionItem("ok", None, None, "5")]
    assert m.open_questions == ["real com espaços"]


@pytest.mark.parametrize("text", ["sem json aqui", "{ quebrado ", "[1, 2]", "x } y {"])
def test_parse_minutes_rejects_non_objects(text: str) -> None:
    with pytest.raises(MinutesParseError):
        parse_minutes(text)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("14:00:05", "14:00:05"),
        ("[14:00:05]", "14:00:05"),
        ("9:05:00", "09:05:00"),
        (" 14:00:05 ", "14:00:05"),
        ("25:00:00", None),
        ("14:60:00", None),
        ("14h00", None),
        ("", None),
    ],
)
def test_normalize_hms(raw: str, expected: str | None) -> None:
    assert normalize_hms(raw) == expected


def test_hallucinated_timestamps_are_dropped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    m = parse_minutes(json.dumps(GOOD))
    with caplog.at_level(logging.WARNING, logger="sombra.summary.minutes"):
        kept, dropped = validate_actions(m, {"14:00:05"})
    assert [a.description for a in kept.actions] == ["Estimar o painel de relatórios"]
    assert [a.ref for a in dropped] == ["23:59:59"]
    assert "23:59:59" in caplog.text


# --- rendering -------------------------------------------------------------------------


def test_render_minutes_ptbr_sections() -> None:
    md = render_minutes(
        Minutes(
            summary="Resumo.",
            decisions=["D1"],
            actions=[
                ActionItem("Estimar painel", "EU", "sexta", "14:00:05"),
                ActionItem("Verificar backup", None, None, "14:10:00"),
            ],
            open_questions=[],
        )
    )
    assert md.splitlines()[0] == "### Resumo"
    assert "### Decisões\n\n- D1" in md
    assert "- [ ] Estimar painel — responsável: EU · prazo: sexta · [14:00:05]" in md
    assert "- [ ] Verificar backup — responsável: sem responsável definido · [14:10:00]" in md
    assert "### Perguntas em aberto\n\n- Nenhuma pergunta em aberto." in md


def test_render_empty_minutes() -> None:
    md = render_minutes(Minutes(summary=""))
    assert "Sem resumo." in md
    assert "Nenhuma decisão registrada." in md
    assert "Nenhum item de ação registrado." in md


# --- generation --------------------------------------------------------------------------


def test_write_minutes_end_to_end_with_fixture(tmp_path: Path) -> None:
    (tmp_path / "transcript.md").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    append_epoch(tmp_path / "summary.md", EpochSection(1, "14:25:00", "resumo da época"))

    def reply(system: str, user: str) -> str:
        assert system == prompts.MINUTES_SYSTEM
        first = transcript_times(user)[0]
        data = dict(GOOD)
        data["acoes"] = [
            {
                "descricao": "Atualizar o roadmap",
                "responsavel": "EU",
                "prazo": "amanhã",
                "ref": first,
            },
            {"descricao": "Inventada", "responsavel": "Zé", "prazo": None, "ref": "03:03:03"},
        ]
        return json.dumps(data, ensure_ascii=False)

    model = FakeModel(reply)
    result = write_minutes(tmp_path, model, day=DAY)
    assert result.calls == 1
    assert [a.description for a in result.minutes.actions] == ["Atualizar o roadmap"]
    assert [a.ref for a in result.dropped] == ["03:03:03"]
    assert result.usage.output_tokens == 10
    ata = read_minutes_section(tmp_path / "summary.md")
    assert ata is not None and "[14:00:00]" in ata and "03:03:03" not in ata
    # the header block is not sent; only timeline lines
    assert "participantes" not in model.calls[0].user
    assert read_current_epoch(tmp_path / "summary.md") == EpochSection(
        1, "14:25:00", "resumo da época"
    )


def test_every_rendered_action_timestamp_exists_in_transcript(tmp_path: Path) -> None:
    meeting = _meeting(tmp_path, synthetic_lines(0.5))

    def reply(system: str, user: str) -> str:
        times = transcript_times(user)
        refs = [times[3], "14:00:01", times[-1], "[" + times[10] + "]", "amanhã"]
        acoes = [{"descricao": f"a{i}", "ref": r} for i, r in enumerate(refs)]
        return json.dumps({"resumo": "r", "acoes": acoes})

    result = write_minutes(meeting, FakeModel(reply), day=DAY)
    known = {line.hms for line in read_transcript(meeting / "transcript.md", day=DAY)}
    rendered = re.findall(r"\[(\d{2}:\d{2}:\d{2})\]$", result.markdown, flags=re.M)
    assert len(rendered) == 3
    assert set(rendered) <= known
    assert len(result.dropped) == 2


def test_three_hour_transcript_is_map_reduced(tmp_path: Path) -> None:
    lines = synthetic_lines(3)  # 2700 lines
    meeting = _meeting(tmp_path, lines)
    context = 30_000
    max_tokens = 2_000

    def reply(system: str, user: str) -> str:
        if system == prompts.MINUTES_SYSTEM:
            times = transcript_times(user)
            return json.dumps(
                {
                    "resumo": f"trecho {times[0]}-{times[-1]}",
                    "decisoes": [f"decisão {times[0]}"],
                    "acoes": [{"descricao": f"ação {times[0]}", "ref": times[0]}],
                    "perguntas_abertas": [],
                }
            )
        assert system == prompts.REDUCE_SYSTEM
        partials = json.loads(user.removeprefix("<parciais>\n").removesuffix("\n</parciais>"))
        return json.dumps(
            {
                "resumo": " + ".join(p["resumo"] for p in partials),
                "decisoes": [d for p in partials for d in p["decisoes"]],
                "acoes": [a for p in partials for a in p["acoes"]],
                "perguntas_abertas": [],
            }
        )

    model = FakeModel(reply)
    result = write_minutes(meeting, model, day=DAY, context_tokens=context, max_tokens=max_tokens)

    maps = [c for c in model.calls if c.system == prompts.MINUTES_SYSTEM]
    reduces = [c for c in model.calls if c.system == prompts.REDUCE_SYSTEM]
    assert len(maps) > 2 and len(reduces) >= 1
    budget = context - max_tokens - 2_000
    # every map call fits the context and together they cover every line, in order
    seen: list[str] = []
    for call in maps:
        assert estimate_tokens(call.user) <= budget + 50
        seen.extend(transcript_times(call.user))
    assert seen == [ln[1:9] for ln in lines]
    assert result.calls == len(maps) + len(reduces)
    assert len(result.minutes.actions) == len(maps)
    assert result.minutes.summary.startswith("trecho 14:00:00-")
    assert result.minutes.summary.endswith("-16:59:56")
    assert all(c.max_tokens == max_tokens for c in model.calls)


def test_reduce_recurses_when_partials_do_not_fit(tmp_path: Path) -> None:
    meeting = _meeting(tmp_path, synthetic_lines(1))
    lines = read_transcript(meeting / "transcript.md", day=DAY)

    def reply(system: str, user: str) -> str:
        pad = "x" * 3_000  # makes each partial big so the reduce input overflows
        return json.dumps({"resumo": pad if system == prompts.MINUTES_SYSTEM else "final"})

    model = FakeModel(reply)
    result = generate_minutes(lines, model, context_tokens=4_000, max_tokens=500)
    reduces = [c for c in model.calls if c.system == prompts.REDUCE_SYSTEM]
    assert len(reduces) > 1
    assert result.minutes.summary == "final"


def test_empty_transcript_needs_no_model_call(tmp_path: Path) -> None:
    meeting = _meeting(tmp_path, [])
    model = FakeModel(lambda s, u: pytest.fail("should not be called"))
    result = write_minutes(meeting, model, day=DAY)
    assert result.calls == 0
    assert "Nenhuma fala registrada" in (read_minutes_section(meeting / "summary.md") or "")


def test_model_error_propagates_and_writes_nothing(tmp_path: Path) -> None:
    meeting = _meeting(tmp_path, synthetic_lines(0.1))
    with pytest.raises(MinutesParseError):
        write_minutes(meeting, FakeModel(lambda s, u: "desculpe"), day=DAY)
    assert not (meeting / "summary.md").exists()


def test_transcript_is_fenced_as_data() -> None:
    lines = read_transcript(FIXTURE, day=DAY)
    model = FakeModel(lambda s, u: json.dumps({"resumo": "r"}))
    generate_minutes(lines, model)
    assert model.calls[0].user.startswith("<transcricao>\n[14:00:00]")
    assert "não é instrução" in model.calls[0].system


# --- transcript reading ----------------------------------------------------------------


def test_read_transcript_handles_midnight(tmp_path: Path) -> None:
    path = tmp_path / "t.md"
    path.write_text("header\n[23:59:58] EU: oi\n[00:00:03] OUTROS: tchau\nlixo\n", encoding="utf-8")
    lines = read_transcript(path, day=datetime(2026, 9, 29))
    assert [ln.hms for ln in lines] == ["23:59:58", "00:00:03"]
    assert lines[1].entry.ts > lines[0].entry.ts


def test_chunk_lines_budget() -> None:
    lines = read_transcript(FIXTURE, day=DAY)
    with pytest.raises(ValueError):
        chunk_lines(lines, 0)
    one_each = chunk_lines(lines, 1)
    assert len(one_each) == len(lines)
    assert chunk_lines([], 100) == []
