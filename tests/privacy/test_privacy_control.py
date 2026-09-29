import asyncio
import os
import shutil
import socket
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from sombra.privacy import control
from sombra.privacy.control import (
    ControlServer,
    ControlSocketError,
    default_socket_path,
    peer_uid,
    send_command,
)
from sombra.privacy.pause import PauseController


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    # Short path: AF_UNIX paths are limited to ~104 bytes and macOS $TMPDIR is long.
    d = Path(tempfile.mkdtemp(prefix="sb", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


async def ask(command: str, path: Path) -> str:
    return await asyncio.to_thread(send_command, command, path)


async def test_pause_resume_over_socket(sock_dir: Path) -> None:
    c = PauseController()
    path = sock_dir / "run" / "control.sock"
    async with ControlServer(c, path):
        assert await ask("status", path) == "ok running"
        assert await ask("pause", path) == "ok paused"
        assert c.is_paused
        assert await ask("toggle", path) == "ok running"
        assert await ask("pause", path) == "ok paused"
        assert await ask("resume", path) == "ok running"
        assert not c.is_paused
    assert not path.exists()


async def test_socket_and_dir_permissions(sock_dir: Path) -> None:
    path = sock_dir / "run" / "control.sock"
    async with ControlServer(PauseController(), path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


async def test_loose_dir_permissions_are_tightened(sock_dir: Path) -> None:
    d = sock_dir / "run"
    d.mkdir(mode=0o755)
    d.chmod(0o755)
    async with ControlServer(PauseController(), d / "c.sock"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700


async def test_symlinked_dir_refused(sock_dir: Path) -> None:
    real = sock_dir / "real"
    real.mkdir()
    (sock_dir / "run").symlink_to(real, target_is_directory=True)
    with pytest.raises(ControlSocketError):
        await ControlServer(PauseController(), sock_dir / "run" / "c.sock").start()


async def test_existing_regular_file_never_replaced(sock_dir: Path) -> None:
    path = sock_dir / "c.sock"
    path.write_text("keep")
    with pytest.raises(ControlSocketError):
        await ControlServer(PauseController(), path).start()
    assert path.read_text() == "keep"


async def test_stale_socket_replaced(sock_dir: Path) -> None:
    path = sock_dir / "c.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.close()  # bound but nobody listening
    async with ControlServer(PauseController(), path):
        assert await ask("status", path) == "ok running"


async def test_second_server_refused(sock_dir: Path) -> None:
    path = sock_dir / "c.sock"
    async with ControlServer(PauseController(), path):
        with pytest.raises(ControlSocketError, match="already listening"):
            await ControlServer(PauseController(), path).start()


async def test_unknown_and_garbage_commands(sock_dir: Path) -> None:
    path = sock_dir / "c.sock"
    c = PauseController()
    async with ControlServer(c, path):
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b"rm -rf /\n")
        await writer.drain()
        assert await reader.readline() == b"error unknown command\n"
        writer.close()
        await writer.wait_closed()
    assert not c.is_paused


async def test_other_user_rejected(sock_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = sock_dir / "c.sock"
    c = PauseController()
    monkeypatch.setattr(control, "peer_uid", lambda sock: os.getuid() + 1)
    async with ControlServer(c, path):
        assert await ask("pause", path) == "error forbidden"
    assert not c.is_paused


async def test_unknown_peer_rejected(sock_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = sock_dir / "c.sock"
    c = PauseController()
    monkeypatch.setattr(control, "peer_uid", lambda sock: None)
    async with ControlServer(c, path):
        assert await ask("pause", path) == "error forbidden"
    assert not c.is_paused


def test_peer_uid_is_current_user() -> None:
    a, b = socket.socketpair(socket.AF_UNIX)
    with a, b:
        uid = peer_uid(a)
    assert uid in (os.getuid(), None)  # None only where the OS can't say


def test_peer_uid_unknown_platform() -> None:
    a, b = socket.socketpair(socket.AF_UNIX)
    with a, b:
        assert peer_uid(a, platform="plan9") is None


def test_peer_uid_os_error() -> None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.close()
    assert peer_uid(s, platform="linux") is None


def test_client_errors(sock_dir: Path) -> None:
    with pytest.raises(ValueError):
        send_command("shutdown", sock_dir / "c.sock")
    with pytest.raises(ControlSocketError, match="no running Sombra session"):
        send_command("pause", sock_dir / "nope.sock")


def test_default_socket_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert default_socket_path() == tmp_path / "sombra" / "control.sock"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert default_socket_path() == Path.home() / ".sombra" / "run" / "control.sock"


async def test_close_is_idempotent(sock_dir: Path) -> None:
    server = ControlServer(PauseController(), sock_dir / "c.sock")
    await server.close()
    await server.start()
    await server.close()
    await server.close()
