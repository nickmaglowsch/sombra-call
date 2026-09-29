import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sombra.config import (
    AudioConfig,
    ConfigError,
    ModelsConfig,
    RetentionConfig,
    UserConfig,
    UserIdentity,
    config_dir,
    default_config_path,
    list_profiles,
    load_meeting_config,
    load_profile,
    load_user_config,
    profiles_dir,
)
from sombra.contracts import AutonomyLevel
from sombra.store import create_meeting

START = datetime(2026, 9, 29, 14, 30, tzinfo=timezone(timedelta(hours=-3)))


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# --- paths ---------------------------------------------------------------------------


def test_paths_follow_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_dir() == tmp_path / "sombra"
    assert default_config_path() == tmp_path / "sombra" / "config.toml"
    assert profiles_dir() == tmp_path / "sombra" / "profiles"


def test_paths_default_to_dot_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_dir() == tmp_path / ".config" / "sombra"


# --- user config ---------------------------------------------------------------------


def test_missing_config_falls_back_to_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    cfg = load_user_config()
    assert cfg == UserConfig(meetings_root=tmp_path / "Sombra" / "meetings")
    assert cfg.autonomy_level is AutonomyLevel.L1
    assert cfg.capture_interval_s == 5.0
    assert cfg.retention == RetentionConfig(frames_days=7, transcripts_days=30)
    assert cfg.audio == AudioConfig()
    assert cfg.models == ModelsConfig()


def test_full_user_config(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.toml",
        """
        meetings_root = "/data/meetings"
        autonomy_level = "l2"
        capture_interval_s = 8

        [user]
        name = "Nick"
        aliases = ["Nicolas", "Nick"]

        [retention]
        frames_days = 3
        transcripts_days = 60

        [audio]
        mic = "MacBook Pro Microphone"
        system = "BlackHole 2ch"

        [models]
        stt = "small"
        agent = "opus"
        summary = "haiku"
        """,
    )
    cfg = load_user_config(path)
    assert cfg == UserConfig(
        meetings_root=Path("/data/meetings"),
        autonomy_level=AutonomyLevel.L2,
        capture_interval_s=8.0,
        user=UserIdentity(name="Nick", aliases=("Nicolas", "Nick")),
        retention=RetentionConfig(frames_days=3, transcripts_days=60),
        audio=AudioConfig(mic="MacBook Pro Microphone", system="BlackHole 2ch"),
        models=ModelsConfig(stt="small", agent="opus", summary="haiku"),
    )
    assert cfg.user.all_aliases == ("Nick", "Nicolas")


def test_partial_config_keeps_other_defaults(tmp_path: Path) -> None:
    path = _write(tmp_path / "c.toml", '[user]\nname = "Ana"\n')
    cfg = load_user_config(path)
    assert cfg.user.all_aliases == ("Ana",)
    assert cfg.retention == RetentionConfig()
    assert cfg.meetings_root == Path("~/Sombra/meetings").expanduser()


@pytest.mark.parametrize(
    ("body", "key", "fragment"),
    [
        ("capture_interval_s = 0.5\n", "capture_interval_s", "between"),
        ('capture_interval_s = "5"\n', "capture_interval_s", "a number"),
        ("capture_interval_s = true\n", "capture_interval_s", "a number"),
        ('autonomy_level = "L9"\n', "autonomy_level", "one of L0, L1, L2, L3"),
        ("meetings_root = 3\n", "meetings_root", "a string"),
        ("[retention]\nframes_days = 0\n", "retention.frames_days", ">= 1"),
        ('[retention]\nframes_days = "7"\n', "retention.frames_days", "an integer"),
        ('[user]\naliases = "Nick"\n', "user.aliases", "a list of strings"),
        ("[user]\naliases = [1]\n", "user.aliases", "a list of strings"),
        ('user = "Nick"\n', "user", "a table"),
        ('[audio]\nmicrophone = "x"\n', "audio.microphone", "unknown key"),
        ("colour = 1\n", "colour", "unknown key"),
    ],
)
def test_config_errors_name_file_and_key(
    tmp_path: Path, body: str, key: str, fragment: str
) -> None:
    path = _write(tmp_path / "config.toml", body)
    with pytest.raises(ConfigError) as exc:
        load_user_config(path)
    assert exc.value.path == path
    assert exc.value.key == key
    assert str(exc.value).startswith(f"{path}: {key}: ")
    assert fragment in str(exc.value)


def test_invalid_toml_names_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.toml", "meetings_root = \n")
    with pytest.raises(ConfigError, match="invalid TOML") as exc:
        load_user_config(path)
    assert str(path) in str(exc.value)


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ('api_key = "sk-ant-xxx"\n', "api_key"),
        ('[models]\nanthropic_api_key = "x"\n', "models.anthropic_api_key"),
        ('[models]\nopenai-apikey = "x"\n', "models.openai-apikey"),
        ('[auth]\ntoken = "x"\n', "auth.token"),
        ('[user]\npassword = "x"\n', "user.password"),
        ('[a.b]\nclient_secret = "x"\n', "a.b.client_secret"),
    ],
)
def test_api_key_like_keys_point_to_sombra_auth(tmp_path: Path, body: str, key: str) -> None:
    path = _write(tmp_path / "config.toml", body)
    with pytest.raises(ConfigError, match="sombra auth") as exc:
        load_user_config(path)
    assert exc.value.key == key


def test_secret_rejected_in_profile_and_meeting(tmp_path: Path) -> None:
    profile = _write(tmp_path / "profiles" / "p.toml", 'api_key = "x"\n')
    with pytest.raises(ConfigError, match="sombra auth"):
        load_profile("p", profile.parent)
    meeting = _write(tmp_path / "meeting.toml", 'OPENAI_API_KEY = "x"\n')
    with pytest.raises(ConfigError, match="sombra auth"):
        load_meeting_config(meeting)


def test_tokens_like_counts_are_not_secrets(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.toml", "[models]\nmax_tokens = 5\n")
    with pytest.raises(ConfigError, match="unknown key"):
        load_user_config(path)


# --- profiles ------------------------------------------------------------------------


def test_load_profile_resolves_context_paths(tmp_path: Path) -> None:
    d = tmp_path / "profiles"
    _write(
        d / "planning.toml",
        """
        description = "Planejamento semanal"
        context = ["docs/roadmap.md", "/abs/tickets"]
        allowed_topics = ["roadmap", "prazos"]
        """,
    )
    p = load_profile("planning", d)
    assert p.name == "planning"
    assert p.description == "Planejamento semanal"
    assert p.context == (d / "docs/roadmap.md", Path("/abs/tickets"))
    assert p.allowed_topics == ("roadmap", "prazos")


def test_load_profile_missing_and_invalid_name(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no profile 'x'"):
        load_profile("x", tmp_path)
    with pytest.raises(ValueError, match="invalid profile name"):
        load_profile("../etc/passwd", tmp_path)


def test_profile_errors_name_file_and_key(tmp_path: Path) -> None:
    path = _write(tmp_path / "p.toml", "context = 1\n")
    with pytest.raises(ConfigError, match=r"p\.toml: context: expected a list of strings"):
        load_profile("p", tmp_path)
    _write(path, 'contexts = ["a"]\n')
    with pytest.raises(ConfigError, match="contexts: unknown key"):
        load_profile("p", tmp_path)


def test_list_profiles(tmp_path: Path) -> None:
    assert list_profiles(tmp_path / "none") == []
    _write(tmp_path / "b.toml", "")
    _write(tmp_path / "a.toml", 'description = "A"\n')
    (tmp_path / "notes.txt").write_text("ignored")
    assert [(p.name, p.description) for p in list_profiles(tmp_path)] == [("a", "A"), ("b", "")]


# --- meeting.toml --------------------------------------------------------------------


def test_meeting_config_round_trips_store_output(tmp_path: Path) -> None:
    ctx = _write(tmp_path / "notes.md", "n")
    d = create_meeting(
        tmp_path / "m",
        "Revisão Q4",
        "planning",
        context_paths=[ctx],
        aliases=["Nick"],
        autonomy_level=AutonomyLevel.L2,
        allowed_topics=["roadmap"],
        started_at=START,
    )
    for target in (d, d / "meeting.toml"):
        cfg = load_meeting_config(target)
        assert cfg.name == "Revisão Q4"
        assert cfg.started_at == START
        assert cfg.aliases == ("Nick",)
        assert cfg.autonomy_level is AutonomyLevel.L2
        assert cfg.allowed_topics == ("roadmap",)
        assert cfg.profile == "planning"
        assert cfg.context_files == ("context/notes.md",)


def test_meeting_config_requires_name_and_started_at(tmp_path: Path) -> None:
    path = _write(tmp_path / "meeting.toml", "started_at = 2026-09-29T14:30:00-03:00\n")
    with pytest.raises(ConfigError, match="name: required"):
        load_meeting_config(path)
    _write(path, 'name = "x"\n')
    with pytest.raises(ConfigError, match="started_at: required"):
        load_meeting_config(path)
    _write(path, 'name = "x"\nstarted_at = "ontem"\n')
    with pytest.raises(ConfigError, match="started_at: expected a TOML datetime"):
        load_meeting_config(path)
