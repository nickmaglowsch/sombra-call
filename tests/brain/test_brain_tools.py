"""brain.tools: read-only tools confined to the meeting folder (C2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sombra.brain.tools import TOOL_NAMES, MeetingTools, ToolError

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9"


@pytest.fixture
def meeting(tmp_path: Path) -> Path:
    root = tmp_path / "2026-09-29_1430_daily"
    (root / "context").mkdir(parents=True)
    (root / "frames").mkdir()
    (root / "transcript.md").write_text(
        "[14:32:07] EU: acho que dá pra fechar na sexta\n"
        "[14:32:09] OUTROS: Nick, o que você acha desse gráfico?\n"
        '[14:32:10] TELA f0001 "Zoom - Roadmap Q4"\n',
        encoding="utf-8",
    )
    (root / "context" / "roadmap.md").write_text("# Roadmap\nQ4: lançar o beta\n", "utf-8")
    (root / "frames" / "f0001.jpg").write_bytes(JPEG)
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("PRIVATE KEY\n", "utf-8")
    return secret


@pytest.fixture
def tools(meeting: Path) -> MeetingTools:
    return MeetingTools(meeting)


# --- confinement -------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../secret/id_rsa",
        "context/../../secret/id_rsa",
        "context/../transcript.md",  # stays inside, but '..' is refused outright
        "..",
        "/etc/passwd",
        "~/.ssh/id_rsa",
        "~",
        "C:/Windows/win.ini",
        "..\\secret\\id_rsa",
        "",
        "   ",
        "transcript.md\x00.jpg",
    ],
)
def test_read_refuses_paths_outside(tools: MeetingTools, outside: Path, path: str) -> None:
    with pytest.raises(ToolError):
        tools.read(path)


def test_absolute_path_to_a_file_inside_is_still_refused(
    tools: MeetingTools, meeting: Path
) -> None:
    with pytest.raises(ToolError, match="absolute"):
        tools.read(str(meeting / "transcript.md"))


def test_symlinked_file_pointing_outside_is_refused(
    tools: MeetingTools, meeting: Path, outside: Path
) -> None:
    (meeting / "context" / "notes.md").symlink_to(outside / "id_rsa")
    with pytest.raises(ToolError, match="leaves the meeting folder"):
        tools.read("context/notes.md")
    assert "PRIVATE" not in tools.grep("PRIVATE")
    assert "notes.md" not in tools.glob("**/*")


def test_symlinked_dir_pointing_outside_is_refused(
    tools: MeetingTools, meeting: Path, outside: Path
) -> None:
    (meeting / "context" / "ssh").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ToolError):
        tools.read("context/ssh/id_rsa")
    with pytest.raises(ToolError):
        tools.grep("PRIVATE", path="context/ssh")
    assert tools.grep("PRIVATE") == "(no matches)"
    assert "id_rsa" not in tools.glob("context/ssh/*")
    assert "id_rsa" not in tools.glob("**/*")


def test_symlink_inside_the_folder_is_allowed(tools: MeetingTools, meeting: Path) -> None:
    (meeting / "context" / "latest.md").symlink_to(meeting / "context" / "roadmap.md")
    assert "lançar o beta" in tools.read("context/latest.md")


def test_symlink_loop_is_refused(tools: MeetingTools, meeting: Path) -> None:
    (meeting / "loop").symlink_to(meeting / "loop")
    with pytest.raises(ToolError):
        tools.read("loop")


def test_missing_file(tools: MeetingTools) -> None:
    with pytest.raises(ToolError, match="not found"):
        tools.read("context/nope.md")


def test_meeting_dir_must_exist(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        MeetingTools(tmp_path / "missing")
    (tmp_path / "file").write_text("x")
    with pytest.raises(NotADirectoryError):
        MeetingTools(tmp_path / "file")


# --- read --------------------------------------------------------------------------


def test_read_numbers_lines_and_pages(tools: MeetingTools) -> None:
    out = tools.read("transcript.md")
    assert out.splitlines()[0] == "     1\t[14:32:07] EU: acho que dá pra fechar na sexta"
    page = tools.read("transcript.md", offset=1, limit=1)
    assert page == "     2\t[14:32:09] OUTROS: Nick, o que você acha desse gráfico?"
    assert tools.read("transcript.md", offset=10) == "(no lines in range)"


def test_read_truncates_long_files(meeting: Path) -> None:
    (meeting / "context" / "big.md").write_text("\n".join("x" * 50 for _ in range(100)))
    out = MeetingTools(meeting, max_read_chars=500).read("context/big.md")
    assert "truncated" in out.splitlines()[-1]
    assert len(out) < 700


def test_read_refuses_directories_and_binaries(tools: MeetingTools) -> None:
    with pytest.raises(ToolError, match="directory"):
        tools.read("context")
    with pytest.raises(ToolError, match="binary"):
        tools.read("frames/f0001.jpg")


# --- grep / glob -------------------------------------------------------------------


def test_grep_finds_lines_and_skips_binaries(tools: MeetingTools) -> None:
    out = tools.grep("gráfico|beta")
    assert "transcript.md:2: [14:32:09] OUTROS: Nick, o que você acha desse gráfico?" in out
    assert "context/roadmap.md:2: Q4: lançar o beta" in out
    assert "frames" not in out
    assert tools.grep("BETA", ignore_case=True).count("\n") == 0
    assert tools.grep("nada disso") == "(no matches)"


def test_grep_path_and_glob_filters(tools: MeetingTools) -> None:
    assert tools.grep("a", path="context").startswith("context/roadmap.md")
    only_md = tools.grep("a", glob="context/*.md")
    assert only_md and "transcript" not in only_md
    assert tools.grep("a", path="transcript.md").startswith("transcript.md:1:")


def test_grep_rejects_bad_patterns(tools: MeetingTools) -> None:
    with pytest.raises(ToolError, match="regular expression"):
        tools.grep("(")
    with pytest.raises(ToolError, match="longer"):
        tools.grep("a" * 201)
    with pytest.raises(ToolError):
        tools.grep("a", glob="../*")


def test_grep_stops_at_max_matches(meeting: Path) -> None:
    (meeting / "context" / "many.md").write_text("hit\n" * 50)
    out = MeetingTools(meeting, max_matches=5).grep("hit")
    assert out.count("\n") == 5 and "stopped" in out


def test_glob_lists_relative_sorted(tools: MeetingTools) -> None:
    assert tools.glob("**/*.md").splitlines() == ["context/roadmap.md", "transcript.md"]
    assert tools.glob("frames/*.jpg") == "frames/f0001.jpg"
    assert tools.glob("*.pdf") == "(no files)"


@pytest.mark.parametrize("pattern", ["/etc/*", "../*", "~/.ssh/*", "context/../../*", ""])
def test_glob_refuses_escaping_patterns(tools: MeetingTools, pattern: str) -> None:
    with pytest.raises(ToolError):
        tools.glob(pattern)


def test_glob_caps_results(meeting: Path) -> None:
    for i in range(12):
        (meeting / "context" / f"n{i:02d}.md").write_text("x")
    out = MeetingTools(meeting, max_glob_results=10).glob("context/*.md").splitlines()
    assert len(out) == 11 and out[-1] == "[3 more not shown]"


# --- frames ------------------------------------------------------------------------


def test_view_frame_serves_from_frames_only(tools: MeetingTools) -> None:
    blocks = tools.view_frame("f0001")
    assert blocks[0] == {"type": "text", "text": "TELA f0001:"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["media_type"] == "image/jpeg"


@pytest.mark.parametrize("frame_id", ["f1", "../transcript", "f0001/../../x", "frames/f0001", 7])
def test_view_frame_rejects_bad_ids(tools: MeetingTools, frame_id: object) -> None:
    with pytest.raises(ToolError):
        tools.view_frame(frame_id)  # type: ignore[arg-type]


def test_view_frame_missing(tools: MeetingTools) -> None:
    with pytest.raises(ToolError, match="not found"):
        tools.view_frame("f0999")


def test_view_frame_symlink_out_of_frames_is_refused(
    tools: MeetingTools, meeting: Path, outside: Path
) -> None:
    (meeting / "frames" / "f0002.jpg").symlink_to(outside / "id_rsa")
    with pytest.raises(ToolError):
        tools.view_frame("f0002")
    (meeting / "frames" / "f0003.jpg").symlink_to(meeting / "transcript.md")
    with pytest.raises(ToolError, match="not in frames"):
        tools.view_frame("f0003")


# --- dispatch and specs ------------------------------------------------------------


def test_run_dispatches_every_tool(tools: MeetingTools) -> None:
    assert "roadmap" in tools.run("glob", {"pattern": "context/*"})[0]["text"]
    assert "beta" in tools.run("grep", {"pattern": "beta", "glob": "context/*.md"})[0]["text"]
    assert "Roadmap" in tools.run("read", {"path": "context/roadmap.md", "limit": 1})[0]["text"]
    assert tools.run("view_frame", {"frame_id": "f0001"})[1]["type"] == "image"


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("bash", {"command": "rm -rf ~"}),
        ("write", {"path": "transcript.md", "content": ""}),
        ("web_fetch", {"url": "https://example.com"}),
        ("read", {}),
        ("read", {"path": "transcript.md", "offset": "abc"}),
        ("grep", {"pattern": 3}),
        ("read", ["transcript.md"]),
    ],
)
def test_run_refuses_unknown_tools_and_bad_args(
    tools: MeetingTools, name: str, args: object
) -> None:
    with pytest.raises(ToolError):
        tools.run(name, args)  # type: ignore[arg-type]


def test_specs_are_read_only_sorted_and_stable() -> None:
    specs = MeetingTools.specs()
    assert tuple(s["name"] for s in specs) == TOOL_NAMES == tuple(sorted(TOOL_NAMES))
    assert json.dumps(specs, sort_keys=True) == json.dumps(MeetingTools.specs(), sort_keys=True)
    specs[0]["name"] = "mutated"
    assert MeetingTools.specs()[0]["name"] == "glob"
    for spec in specs:
        assert spec["input_schema"]["additionalProperties"] is False
