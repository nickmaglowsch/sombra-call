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
