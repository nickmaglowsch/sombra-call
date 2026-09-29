from pathlib import Path

import pytest
from tags import (
    TagError,
    check_artifacts,
    is_latest,
    main,
    parse_tag,
    previous_tag,
    release_tags,
)


@pytest.mark.parametrize(
    ("tag", "version", "prerelease"),
    [
        ("v0.1.0", "0.1.0", False),
        ("v1.12.3", "1.12.3", False),
        ("v0.2.0-rc1", "0.2.0rc1", True),
        ("v0.2.0-rc.2", "0.2.0rc2", True),
        ("v0.2.0-beta3", "0.2.0b3", True),
        ("v0.2.0-alpha0", "0.2.0a0", True),
    ],
)
def test_parse_tag(tag: str, version: str, prerelease: bool) -> None:
    parsed = parse_tag(tag)
    assert parsed.version == version
    assert parsed.prerelease is prerelease


@pytest.mark.parametrize(
    "tag", ["0.1.0", "v0.1", "v01.0.0", "v0.1.0-rc", "v0.1.0-dev1", "v0.1.0+local", "vX.Y.Z"]
)
def test_parse_tag_rejects_non_release_tags(tag: str) -> None:
    with pytest.raises(TagError):
        parse_tag(tag)


def test_release_tags_sorts_semver_with_prereleases_before_final() -> None:
    names = ["v0.10.0", "v0.2.0", "v0.2.0-rc1", "v0.2.0-beta2", "nightly", "v0.2.0-rc10"]
    assert [t.name for t in release_tags(names)] == [
        "v0.2.0-beta2",
        "v0.2.0-rc1",
        "v0.2.0-rc10",
        "v0.2.0",
        "v0.10.0",
    ]


TAGS = ["v0.1.0", "v0.2.0-rc1", "v0.2.0-rc2", "v0.2.0", "v0.3.0-beta1"]


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("v0.1.0", None),
        ("v0.2.0-rc1", "v0.1.0"),
        ("v0.2.0-rc2", "v0.2.0-rc1"),
        ("v0.2.0", "v0.1.0"),  # a final release skips its own release candidates
        ("v0.3.0-beta1", "v0.2.0"),
        ("v0.3.0", "v0.2.0"),
    ],
)
def test_previous_tag(current: str, expected: str | None) -> None:
    prev = previous_tag(parse_tag(current), TAGS)
    assert (prev.name if prev else None) == expected


def _dist(tmp_path: Path, *names: str) -> Path:
    for name in names:
        (tmp_path / name).write_bytes(b"x")
    return tmp_path


def test_check_artifacts_matches_the_tag(tmp_path: Path) -> None:
    dist = _dist(tmp_path, "sombra-0.2.0rc1-py3-none-any.whl", "sombra-0.2.0rc1.tar.gz", "x.sh")
    assert check_artifacts(parse_tag("v0.2.0-rc1"), dist) == [
        "sombra-0.2.0rc1-py3-none-any.whl",
        "sombra-0.2.0rc1.tar.gz",
    ]


def test_check_artifacts_rejects_a_dev_build(tmp_path: Path) -> None:
    dist = _dist(tmp_path, "sombra-0.2.1.dev3+g1a2b3c-py3-none-any.whl", "sombra-0.2.0.tar.gz")
    with pytest.raises(TagError, match=r"has version 0\.2\.1\.dev3"):
        check_artifacts(parse_tag("v0.2.0"), dist)


def test_check_artifacts_needs_wheel_and_sdist(tmp_path: Path) -> None:
    with pytest.raises(TagError, match=r"no wheel.*no sdist"):
        check_artifacts(parse_tag("v0.2.0"), tmp_path)


def test_cli_writes_github_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "dist").mkdir()
    dist = _dist(tmp_path / "dist", "sombra-0.3.0b1-py3-none-any.whl", "sombra-0.3.0b1.tar.gz")
    out = tmp_path / "gh_output"
    args = ["check", "v0.3.0-beta1", "--dist", str(dist), "--github-output", str(out)]
    assert main(args) == 0
    assert out.read_text() == "version=0.3.0b1\nprerelease=true\n"
    assert "prerelease=true" in capsys.readouterr().out


def test_cli_fails_on_bad_tag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "release-1"]) == 1
    assert "not a release tag" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("v0.3.0", True),
        ("v0.2.1", False),  # a patch to an older line never becomes releases/latest
        ("v0.4.0-rc1", False),  # pre-releases are never "Latest"
        ("v0.3.1", True),
    ],
)
def test_is_latest(current: str, expected: bool) -> None:
    tags = ["v0.1.0", "v0.2.0", "v0.3.0", "v0.4.0-rc1", current]
    assert is_latest(parse_tag(current), tags) is expected


def test_cli_reports_latest(tmp_path: Path) -> None:
    out = tmp_path / "gh_output"
    args = ["check", "v0.2.1", "--github-output", str(out), "--tags", "v0.2.0", "v0.3.0"]
    assert main(args) == 0
    assert out.read_text().splitlines()[-1] == "latest=false"
    assert main(["check", "v0.4.0", "--github-output", str(out), "--tags"]) == 0
    assert out.read_text().splitlines()[-1] == "latest=true"
