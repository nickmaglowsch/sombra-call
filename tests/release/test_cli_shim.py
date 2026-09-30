"""The CLI shim (``packaging/macos/shim/sombra.c``): compiled and run against a fake app.

On macOS it also sets the responsibility-disclaim attribute; whether TCC then lists
Sombra is checked by ``sombra doctor`` in the release workflow's app smoke job, and on
a real Mac by hand (ADR 0050). Here we check it is a faithful pass-through: arguments,
stdin/stdout, environment, exit status and signals, from a symlink on PATH.
"""

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import build
import pytest

CC = os.environ.get("CC") or shutil.which("cc") or shutil.which("clang")
pytestmark = pytest.mark.skipif(CC is None, reason="no C compiler")

FAKE_APP = """#!/bin/sh
case "$1" in
  --exit) exit "$2" ;;
  --kill) kill -TERM $$; sleep 5 ;;
esac
printf 'argv0=%s\\n' "$0"
for a in "$@"; do printf 'arg=%s\\n' "$a"; done
printf 'env=%s\\n' "${SOMBRA_SHIM_TEST:-}"
cat
"""


@pytest.fixture(scope="module")
def shim_on_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("shim")
    bundle = root / "Applications" / "Sombra.app"
    shim = bundle / "Contents" / "Helpers" / "sombra"
    shim.parent.mkdir(parents=True)
    target = bundle / "Contents" / "MacOS" / "Sombra"
    target.parent.mkdir(parents=True)
    target.write_text(FAKE_APP, encoding="utf-8")
    target.chmod(0o755)
    assert CC is not None
    cmd = build.shim_command(CC, shim, darwin=False)
    if sys.platform == "darwin":
        cmd.remove("-ldl")
    subprocess.run(cmd, check=True)  # noqa: S603  # our compiler argv, no shell
    link = root / "bin" / "sombra"
    link.parent.mkdir()
    link.symlink_to(shim)
    return link


def _run(link: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # the shim we just built
        [str(link), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={**os.environ, "SOMBRA_SHIM_TEST": "passed through"},
    )


def test_passes_arguments_stdin_and_environment(shim_on_path: Path) -> None:
    result = _run(shim_on_path, "doctor", "--json", "with space", stdin="from stdin\n")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    # Started as the bundle's real executable, resolved through the symlink.
    assert lines[0].startswith("argv0=") and lines[0].endswith("/Sombra.app/Contents/MacOS/Sombra")
    assert lines[1:] == [
        "arg=doctor",
        "arg=--json",
        "arg=with space",
        "env=passed through",
        "from stdin",
    ]


@pytest.mark.parametrize("code", [0, 1, 3, 42])
def test_exit_status_is_the_apps(shim_on_path: Path, code: int) -> None:
    assert _run(shim_on_path, "--exit", str(code)).returncode == code


def test_signal_that_kills_the_app_kills_the_shim(shim_on_path: Path) -> None:
    assert _run(shim_on_path, "--kill").returncode == -signal.SIGTERM


def test_missing_app_executable_is_a_clear_error(tmp_path: Path, shim_on_path: Path) -> None:
    lonely = tmp_path / "Helpers" / "sombra"
    lonely.parent.mkdir()
    shutil.copy2(shim_on_path.resolve(), lonely)
    result = _run(lonely)
    assert result.returncode == 127
    assert "Sombra.app is incomplete" in result.stderr
