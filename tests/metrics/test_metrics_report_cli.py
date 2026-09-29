"""`sombra report`: text table, --json, --markdown, --prices, --missed."""

import json
from pathlib import Path

import pytest

from sombra.cli import main
from sombra.metrics import Metric
from sombra.metrics.render import format_target, format_value

FIXTURES = Path(__file__).parent / "fixtures"
PRICES = """
[models."claude-x"]
input = 3.0
output = 15.0
cache_read = 0.3
cache_creation = 3.75

[models."claude-small"]
input = 1.0
output = 5.0
"""


@pytest.fixture
def prices(tmp_path: Path) -> Path:
    path = tmp_path / "prices.toml"
    path.write_text(PRICES, encoding="utf-8")
    return path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["report", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_text_table(capsys: pytest.CaptureFixture[str], prices: Path) -> None:
    code, out, _ = run(capsys, str(FIXTURES / "full"), "--prices", str(prices))
    assert code == 0
    assert "== full ==" in out
    assert "Latency p50 (question → overlay)" in out
    assert "≤ 6 s" in out and "5.0 s" in out
    assert "50.0%" in out and "fail" in out
    assert "US$ 0.02/h" in out
    assert "L3 readiness: 4/50 L2 answers, 50.0% approved unedited" in out
    assert "not ready" in out
    assert "ignored 2 unknown events, 1 bad lines" in out
    assert "== all ==" not in out  # single meeting: no aggregate table


def test_json_for_several_meetings(capsys: pytest.CaptureFixture[str], prices: Path) -> None:
    code, out, _ = run(
        capsys,
        str(FIXTURES / "full"),
        str(FIXTURES / "errors_only"),
        "--json",
        "--prices",
        str(prices),
    )
    assert code == 0
    data = json.loads(out)
    assert [m["name"] for m in data["meetings"]] == ["full", "errors_only"]
    metrics = {m["key"]: m for m in data["meetings"][0]["metrics"]}
    assert metrics["latency_p95_s"] == {
        "key": "latency_p95_s",
        "label": "Latency p95 (question → overlay)",
        "value": 12.0,
        "unit": "s",
        "target": "<= 10",
        "status": "fail",
    }
    assert data["aggregate"]["meetings"] == ["full", "errors_only"]
    assert data["aggregate"]["l3"]["answers"] == 4
    assert "missed_triggers" not in data


def test_markdown_with_aggregate_and_missed(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(
        capsys,
        str(FIXTURES / "full"),
        str(FIXTURES / "empty"),
        "--markdown",
        "--missed",
        "--alias",
        "Nick",
    )
    assert code == 0
    assert "### Sombra metrics: full" in out
    assert "### Sombra metrics: all" in out
    assert "| Metric | Target | Value | Status |" in out
    assert "❌ fail" in out and "✅ pass" in out
    assert "- cost unknown: no price for claude-small, claude-x" in out
    assert "**L3 readiness:" in out
    assert "### Possible missed triggers (label by hand)" in out
    assert "- full: 2" in out and "- empty: 0" in out
    assert "[14:50:30] OUTROS: o Nick falou isso ontem  [alias: Nick]" in out


def test_json_missed(capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(capsys, str(FIXTURES / "full"), "--json", "--missed")
    missed = json.loads(out)["missed_triggers"]["full"]
    assert [m["alias"] for m in missed] == ["Nick", "Nick"]


def test_text_missed(capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(capsys, str(FIXTURES / "full"), "--missed")
    assert "== Possible missed triggers (label by hand) ==" in out
    assert "  full: 2" in out


def test_default_is_current_directory(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(FIXTURES / "errors_only")
    code, out, _ = run(capsys)
    assert code == 0
    assert "== errors_only ==" in out


def test_missing_meeting_and_bad_prices(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, _, err = run(capsys, str(tmp_path / "nope"))
    assert code == 2
    assert "not a meeting folder" in err

    bad = tmp_path / "bad.toml"
    bad.write_text("[models\n", encoding="utf-8")
    code, _, err = run(capsys, str(FIXTURES / "full"), "--prices", str(bad))
    assert code == 2
    assert "bad price table" in err
    code, _, err = run(capsys, str(FIXTURES / "full"), "--prices", str(tmp_path / "none.toml"))
    assert code == 2


def test_json_and_markdown_are_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["report", "--json", "--markdown"])
    capsys.readouterr()


@pytest.mark.parametrize(
    ("unit", "target", "op", "value", "shown_target", "shown_value"),
    [
        ("s", 6.0, "<=", 5.25, "≤ 6 s", "5.2 s"),
        ("%", 70.0, ">=", 71.0, "≥ 70%", "71.0%"),
        ("/h", 2.0, "<=", 1.5, "≤ 2/h", "1.5/h"),
        ("US$/h", 1.0, "<=", 0.4567, "≤ US$ 1/h", "US$ 0.46/h"),
        ("", 3.0, "<=", 2.0, "≤ 3", "2"),
        ("", None, None, None, "—", "—"),
    ],
)
def test_formatting(
    unit: str,
    target: float | None,
    op: str | None,
    value: float | None,
    shown_target: str,
    shown_value: str,
) -> None:
    m = Metric("k", "K", value, unit, target, op)  # type: ignore[arg-type]
    assert format_target(m) == shown_target
    assert format_value(m) == shown_value
