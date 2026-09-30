"""``scripts/install.sh``: shellcheck-clean, and its argument checks fail fast.

The full install/upgrade/uninstall runs in ``.github/workflows/install-smoke.yml``.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_install_sh_is_shellcheck_clean() -> None:
    result = subprocess.run(  # noqa: S603  # fixed argv, no shell
        ["shellcheck", "--shell=sh", str(SCRIPT)],  # noqa: S607  # found on PATH above
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # fixed argv, no shell
        ["sh", str(SCRIPT), *args],  # noqa: S607  # POSIX sh is on every supported OS
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_help_lists_the_flags() -> None:
    result = _run("--help")
    assert result.returncode == 0
    for flag in (
        "--version",
        "--from-wheel",
        "--yes",
        "--no-models",
        "--modify-path",
        "--uninstall",
    ):
        assert flag in result.stdout


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--bogus",), "unknown argument: --bogus"),
        (("--version", "1.2"), "--version must look like vX.Y.Z"),
        (("--version",), "--version needs a value"),
        (("--from-wheel", "/nonexistent/sombra.whl"), "no such file"),
    ],
)
def test_bad_arguments_fail_before_doing_anything(args: tuple[str, ...], message: str) -> None:
    result = _run(*args)
    assert result.returncode == 1
    assert message in result.stderr


def test_from_wheel_rejects_non_wheel(tmp_path: Path) -> None:
    other = tmp_path / "sombra.tar.gz"
    other.write_bytes(b"")
    result = _run("--from-wheel", str(other))
    assert result.returncode == 1
    assert "not a .whl file" in result.stderr


def test_uninstall_deletes_only_downloaded_models_and_keeps_meetings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    uv = fakebin / "uv"
    # A fake uv: `tool dir --bin` names an empty bin dir, `tool list` lists nothing.
    uv.write_text(
        '#!/bin/sh\nif [ "$1 $2" = "tool dir" ]; then echo "$HOME/.local/bin"; fi\nexit 0\n'
    )
    uv.chmod(0o755)
    models = home / ".cache" / "sombra" / "models"
    models.mkdir(parents=True)
    for name in ("ggml-tiny.bin", "ggml-small-q5_1.bin.part", "silero_vad.onnx", "mine.txt"):
        (models / name).write_bytes(b"x")
    meeting = home / "Sombra" / "meetings" / "2026-01-01_0900_x" / "transcript.md"
    meeting.parent.mkdir(parents=True)
    meeting.write_text("[09:00:00] EU: oi\n")
    # SOMBRA_APP_DIR keeps the uninstall away from a real /Applications/Sombra.app.
    env = {
        "HOME": str(home),
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "SOMBRA_APP_DIR": str(tmp_path / "Applications"),
    }

    result = subprocess.run(  # noqa: S603  # fixed argv, no shell
        ["sh", str(SCRIPT), "--uninstall", "--yes"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in models.iterdir()) == ["mine.txt"]
    assert "holds files the installer did not download" in result.stdout
    assert meeting.read_text() == "[09:00:00] EU: oi\n"

    (models / "mine.txt").unlink()
    (models / "ggml-tiny.bin").write_bytes(b"x")
    result = subprocess.run(  # noqa: S603  # fixed argv, no shell
        ["sh", str(SCRIPT), "--uninstall", "--yes"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not models.exists()
    assert meeting.exists()
