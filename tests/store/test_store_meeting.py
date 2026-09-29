import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sombra.contracts import AutonomyLevel
from sombra.store import create_meeting, read_started_at, slugify

TZ = timezone(timedelta(hours=-3))
START = datetime(2026, 9, 29, 14, 30, 5, tzinfo=TZ)


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Daily time X", "daily-time-x"),
        ("Revisão de Ação — Q4!", "revisao-de-acao-q4"),
        ("   ", "meeting"),
        ("日本語", "meeting"),
        ("a" * 100, "a" * 60),
    ],
)
def test_slugify(name: str, slug: str) -> None:
    assert slugify(name) == slug


def test_create_meeting_layout(tmp_path: Path) -> None:
    d = create_meeting(tmp_path / "meetings", "Daily time X", started_at=START)
    assert d == tmp_path / "meetings" / "2026-09-29_1430_daily-time-x"
    assert (d / "context").is_dir()
    assert (d / "frames").is_dir()
    assert (d / "transcript.md").read_text() == ""
    assert (d / "frames" / "index.jsonl").read_text() == ""
    assert (d / "log.jsonl").read_text() == ""
    assert read_started_at(d) == START


def test_meeting_toml_contents(tmp_path: Path) -> None:
    d = create_meeting(
        tmp_path,
        'Planning "Q4"\n',
        "planning",
        aliases=["Nick", "Nicolas"],
        autonomy_level=AutonomyLevel.L2,
        allowed_topics=["roadmap", "prazos"],
        started_at=START,
    )
    data = tomllib.loads((d / "meeting.toml").read_text(encoding="utf-8"))
    assert data == {
        "name": 'Planning "Q4"\n',
        "started_at": START,
        "aliases": ["Nick", "Nicolas"],
        "autonomy_level": "L2",
        "allowed_topics": ["roadmap", "prazos"],
        "profile": "planning",
        "context_files": [],
    }


def test_no_profile_key_when_none(tmp_path: Path) -> None:
    d = create_meeting(tmp_path, "x", started_at=START)
    assert "profile" not in tomllib.loads((d / "meeting.toml").read_text())


def test_header_written_once(tmp_path: Path) -> None:
    d = create_meeting(tmp_path, "x", started_at=START, header="# Daily\n")
    assert (d / "transcript.md").read_text() == "# Daily\n\n"


def test_name_collision_gets_suffix(tmp_path: Path) -> None:
    a = create_meeting(tmp_path, "x", started_at=START)
    b = create_meeting(tmp_path, "x", started_at=START)
    c = create_meeting(tmp_path, "x", started_at=START)
    assert (a.name, b.name, c.name) == (
        "2026-09-29_1430_x",
        "2026-09-29_1430_x-2",
        "2026-09-29_1430_x-3",
    )


def test_copies_context_files_and_dirs(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "docs" / "sub").mkdir(parents=True)
    (src / "notes.md").write_text("notas")
    (src / "docs" / "a.md").write_text("A")
    (src / "docs" / "sub" / "b.md").write_text("B")
    other = tmp_path / "other"
    other.mkdir()
    (other / "notes.md").write_text("outras notas")

    d = create_meeting(
        tmp_path / "m",
        "x",
        context_paths=[src / "notes.md", src / "docs", other / "notes.md"],
        started_at=START,
    )
    ctx = d / "context"
    assert (ctx / "notes.md").read_text() == "notas"
    assert (ctx / "notes-2.md").read_text() == "outras notas"
    assert (ctx / "docs" / "sub" / "b.md").read_text() == "B"
    files = tomllib.loads((d / "meeting.toml").read_text())["context_files"]
    assert files == [
        "context/notes.md",
        "context/docs/a.md",
        "context/docs/sub/b.md",
        "context/notes-2.md",
    ]


def test_missing_context_path_creates_nothing(tmp_path: Path) -> None:
    root = tmp_path / "m"
    with pytest.raises(FileNotFoundError, match="nope"):
        create_meeting(root, "x", context_paths=[tmp_path / "nope"], started_at=START)
    assert not root.exists()


def test_empty_name_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        create_meeting(tmp_path, "  ")


def test_default_started_at_is_now_and_aware(tmp_path: Path) -> None:
    d = create_meeting(tmp_path, "x")
    started = read_started_at(d)
    assert started.tzinfo is not None
    assert abs(datetime.now().astimezone() - started) < timedelta(minutes=1)


def test_read_started_at_rejects_missing(tmp_path: Path) -> None:
    (tmp_path / "meeting.toml").write_text('name = "x"\n')
    with pytest.raises(ValueError, match="started_at"):
        read_started_at(tmp_path)
