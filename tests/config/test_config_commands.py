from pathlib import Path

import pytest

from sombra.cli import main
from sombra.config import load_meeting_config


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path


def _config(home: Path, body: str) -> None:
    path = home / "cfg" / "sombra" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_new_prints_folder_path(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["new", "Daily time X"]) == 0
    out = capsys.readouterr().out.strip()
    d = Path(out)
    assert d.parent == home / "Sombra" / "meetings"
    assert d.name.endswith("_daily-time-x")
    assert (d / "transcript.md").exists()


def test_new_with_profile_uses_config_and_profile(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _config(
        home,
        f'meetings_root = "{home / "m"}"\nautonomy_level = "L2"\n'
        '[user]\nname = "Nick"\naliases = ["Nicolas"]\n',
    )
    profiles = home / "cfg" / "sombra" / "profiles"
    (profiles / "ctx").mkdir(parents=True)
    (profiles / "ctx" / "roadmap.md").write_text("Q4")
    (profiles / "planning.toml").write_text(
        'context = ["ctx/roadmap.md"]\nallowed_topics = ["roadmap"]\n'
    )

    assert main(["new", "Planning", "--profile", "planning"]) == 0
    d = Path(capsys.readouterr().out.strip())
    assert d.parent == home / "m"
    assert (d / "context" / "roadmap.md").read_text() == "Q4"
    cfg = load_meeting_config(d)
    assert cfg.profile == "planning"
    assert cfg.aliases == ("Nick", "Nicolas")
    assert cfg.allowed_topics == ("roadmap",)
    assert cfg.autonomy_level.value == "L2"


def test_new_explicit_config_path(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = home / "other.toml"
    cfg.write_text(f'meetings_root = "{home / "elsewhere"}"\n')
    assert main(["new", "x", "--config", str(cfg)]) == 0
    assert Path(capsys.readouterr().out.strip()).parent == home / "elsewhere"


def test_new_reports_config_error(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _config(home, 'api_key = "sk-xxx"\n')
    assert main(["new", "x"]) == 2
    err = capsys.readouterr().err
    assert "config.toml: api_key:" in err
    assert "sombra auth" in err


def test_new_missing_profile(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["new", "x", "--profile", "nope"]) == 2
    assert "no profile 'nope'" in capsys.readouterr().err


def test_profiles_list(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["profiles", "list"]) == 0
    assert "no profiles in" in capsys.readouterr().err

    profiles = home / "cfg" / "sombra" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "planning.toml").write_text('description = "Planejamento"\n')
    (profiles / "daily.toml").write_text("")
    assert main(["profiles", "list"]) == 0
    assert capsys.readouterr().out == "daily\nplanning\tPlanejamento\n"


def test_profiles_list_reports_bad_profile(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    profiles = home / "cfg" / "sombra" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "bad.toml").write_text("context = 1\n")
    assert main(["profiles", "list"]) == 2
    assert "bad.toml: context:" in capsys.readouterr().err


def test_profiles_requires_action(home: Path) -> None:
    with pytest.raises(SystemExit):
        main(["profiles"])
