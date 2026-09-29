import os
import subprocess
from pathlib import Path

import pytest
from notes import build_notes, main, render
from tags import parse_tag


def test_render_lists_pr_titles_and_other_commits() -> None:
    subjects = [
        "[brain] Codex backend behind the same Brain interface (#39)",
        "[ui] Approval overlay (approve / edit / discard) (#31)",
        "hotfix without a PR",
    ]
    text = render(parse_tag("v0.2.0"), parse_tag("v0.1.0"), subjects, "o/r")
    assert "- [brain] Codex backend behind the same Brain interface (#39)" in text
    assert "- [ui] Approval overlay (approve / edit / discard) (#31)" in text
    assert "### Other commits\n\n- hotfix without a PR" in text
    assert "https://github.com/o/r/compare/v0.1.0...v0.2.0" in text
    assert "sha256sum -c SHA256SUMS" in text
    assert "Pre-release" not in text


def test_render_first_prerelease_without_prs() -> None:
    text = render(parse_tag("v0.1.0-rc1"), None, [], "o/r")
    assert text.startswith("Pre-release 0.1.0rc1")
    assert "No pull requests merged" in text
    assert "Other commits" not in text
    assert "https://github.com/o/r/commits/v0.1.0-rc1" in text


def test_render_without_repo_has_no_link() -> None:
    assert "Full changelog" not in render(parse_tag("v0.1.0"), None, ["x (#1)"])


def _commit(repo: Path, subject: str, tag: str | None = None) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    run = {"cwd": repo, "check": True, "capture_output": True, "env": env}
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", subject], **run)  # noqa: S603, S607 - fixed git argv in a temp repo
    if tag:
        subprocess.run(["git", "tag", tag], **run)  # noqa: S603, S607 - fixed git argv in a temp repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603, S607 - fixed git argv in a temp repo
    _commit(tmp_path, "Initial scaffold (#1)", "v0.1.0")
    _commit(tmp_path, "[store] Meeting folder (#2)")
    _commit(tmp_path, "[audio] Capture (#3)", "v0.2.0-rc1")
    _commit(tmp_path, "[trigger] Name detection (#4)", "v0.2.0")
    return tmp_path


def test_build_notes_from_git(repo: Path) -> None:
    final = build_notes("v0.2.0", "o/r", cwd=repo)
    assert final.index("(#4)") < final.index("(#3)") < final.index("(#2)")  # newest first
    assert "(#1)" not in final  # a final release starts from the previous final release
    assert "compare/v0.1.0...v0.2.0" in final

    rc = build_notes("v0.2.0-rc1", cwd=repo)
    assert "(#3)" in rc and "(#2)" in rc and "(#4)" not in rc

    first = build_notes("v0.1.0", cwd=repo)
    assert "- Initial scaffold (#1)" in first


def test_cli(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo)
    out = repo / "notes.md"
    assert main(["v0.2.0", "--repo", "o/r", "--output", str(out)]) == 0
    assert "[trigger] Name detection (#4)" in out.read_text()
    assert main(["v0.2.0-rc1"]) == 0
    assert "[audio] Capture (#3)" in capsys.readouterr().out
    assert main(["v9.9.9"]) == 1  # no such tag in git
    assert main(["latest"]) == 1  # not a release tag
    assert "not a release tag" in capsys.readouterr().err
