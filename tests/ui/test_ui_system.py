"""Notification and clipboard command selection; running them never raises."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from sombra.ui import system


def _which(*present: str):  # type: ignore[no-untyped-def]
    return lambda name: f"/usr/bin/{name}" if name in present else None


def test_macos_notification_passes_text_as_argv() -> None:
    argv = system.notification_argv(
        "Sombra", 'x" & do shell script "rm', platform="darwin", which=_which("osascript")
    )
    assert argv is not None
    assert argv[0] == "/usr/bin/osascript"
    assert argv[-2:] == ["Sombra", 'x" & do shell script "rm']  # data, never script text
    assert "display notification (item 2 of argv) with title (item 1 of argv)" in argv


def test_linux_notification_uses_notify_send() -> None:
    argv = system.notification_argv("T", "-b", platform="linux", which=_which("notify-send"))
    assert argv == ["/usr/bin/notify-send", "--app-name=Sombra", "--", "T", "-b"]


def test_no_notification_tool() -> None:
    assert system.notification_argv("T", "b", platform="darwin", which=_which()) is None
    assert system.notification_argv("T", "b", platform="linux", which=_which()) is None


def test_long_notification_is_shortened() -> None:
    argv = system.notification_argv(
        "T", "palavra " * 100, platform="linux", which=_which("notify-send")
    )
    assert argv is not None
    assert len(argv[-1]) == 200
    assert argv[-1].endswith("…")


@pytest.mark.parametrize(
    ("platform", "present", "expected"),
    [
        ("darwin", ("pbcopy",), ["/usr/bin/pbcopy"]),
        ("darwin", (), None),
        ("linux", ("wl-copy", "xclip"), ["/usr/bin/wl-copy"]),
        ("linux", ("xclip",), ["/usr/bin/xclip", "-selection", "clipboard"]),
        ("linux", (), None),
    ],
)
def test_clipboard_argv(
    platform: str, present: tuple[str, ...], expected: list[str] | None
) -> None:
    assert system.clipboard_argv(platform=platform, which=_which(*present)) == expected


async def test_run_feeds_stdin(tmp_path) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "out.txt"
    code = f"import sys, pathlib; pathlib.Path({str(out)!r}).write_bytes(sys.stdin.buffer.read())"
    await system._run([sys.executable, "-c", code], stdin="olá".encode())
    assert out.read_text(encoding="utf-8") == "olá"


async def test_run_swallows_missing_command() -> None:
    await system._run(["/nonexistent/sombra-tool"])


_SLEEPER = (
    "import os, pathlib, sys, time; "
    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _read_pid(path: Path) -> int | None:
    text = path.read_text() if path.exists() else ""
    return int(text) if text else None


async def _pid(path: Path) -> int:
    for _ in range(200):
        if (pid := _read_pid(path)) is not None:
            return pid
        await asyncio.sleep(0.02)
    raise AssertionError("child never started")


async def test_run_times_out_and_kills_the_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(system, "_TIMEOUT_S", 0.5)
    pidfile = tmp_path / "pid"
    await system._run([sys.executable, "-c", _SLEEPER, str(pidfile)])
    assert not _alive(await _pid(pidfile))


async def test_cancel_kills_the_child(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    task = asyncio.create_task(system._run([sys.executable, "-c", _SLEEPER, str(pidfile)]))
    pid = await _pid(pidfile)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _alive(pid)


async def test_system_helpers_without_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system.shutil, "which", lambda name: None)
    await system.system_notify("T", "b")
    await system.system_clipboard("texto")
    await system.no_notify("T", "b")
    await system.no_clipboard("texto")


async def test_system_helpers_run_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], bytes | None]] = []

    async def fake_run(argv: list[str], stdin: bytes | None = None) -> None:
        calls.append((argv, stdin))

    monkeypatch.setattr(system, "_run", fake_run)
    monkeypatch.setattr(system, "notification_argv", lambda t, b: ["notify", t, b])
    monkeypatch.setattr(system, "clipboard_argv", lambda: ["copy"])
    await system.system_notify("T", "b")
    await system.system_clipboard("texto")
    assert calls == [(["notify", "T", "b"], None), (["copy"], b"texto")]
