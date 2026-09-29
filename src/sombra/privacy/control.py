"""Local control socket: ``sombra pause`` / ``sombra resume`` reach the running session.

A Unix domain socket in a private directory (0700), the socket itself 0600, and a
peer-credential check on every connection: only the user running Sombra can pause
or resume it. The protocol is one ASCII line each way::

    -> pause | resume | toggle | status
    <- ok paused | ok running | error <reason>
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import stat
import struct
import sys
from pathlib import Path

from sombra.privacy.pause import PauseController

log = logging.getLogger(__name__)

COMMANDS = ("pause", "resume", "toggle", "status")
_MAX_LINE = 64
_TIMEOUT_S = 2.0

# macOS <sys/un.h>: SOL_LOCAL = 0, LOCAL_PEERCRED = 1; struct xucred starts with
# (u_int cr_version, uid_t cr_uid).
_SOL_LOCAL = 0
_LOCAL_PEERCRED = 0x001


class ControlSocketError(RuntimeError):
    pass


def default_socket_path() -> Path:
    """``$XDG_RUNTIME_DIR/sombra/control.sock``, else ``~/.sombra/run/control.sock``."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) / "sombra" if runtime else Path.home() / ".sombra" / "run"
    return base / "control.sock"


def peer_uid(sock: socket.socket, platform: str = sys.platform) -> int | None:
    """UID of the process on the other end of a Unix socket, or None if the OS won't say."""
    try:
        if platform.startswith("linux"):
            so_peercred = getattr(socket, "SO_PEERCRED", 17)  # typeshed only has it on Linux
            creds = sock.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", creds)
            return int(uid)
        if platform == "darwin":  # pragma: no cover - exercised by macOS CI only
            raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, 76)
            _version, uid = struct.unpack_from("Ii", raw)
            return int(uid)
    except OSError:
        return None
    return None  # pragma: no cover - other platforms


def _prepare_dir(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = directory.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ControlSocketError(f"{directory} is not a real directory")
    if st.st_uid != os.getuid():
        raise ControlSocketError(f"{directory} is owned by another user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        directory.chmod(0o700)


def _clear_stale(path: Path) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        raise ControlSocketError(f"{path} exists and is not a socket; refusing to replace it")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
    except OSError:
        path.unlink()  # nobody listening: a leftover from a crashed session
        return
    finally:
        probe.close()
    raise ControlSocketError(f"another Sombra session is already listening on {path}")


class ControlServer:
    """Serves the control protocol for one ``PauseController``."""

    def __init__(self, controller: PauseController, path: Path | None = None) -> None:
        self.controller = controller
        self.path = path if path is not None else default_socket_path()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        _prepare_dir(self.path.parent)
        _clear_stale(self.path)
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        self.path.chmod(0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            if stat.S_ISSOCK(self.path.lstat().st_mode):
                self.path.unlink()

    async def __aenter__(self) -> ControlServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def handle_command(self, command: str) -> str:
        c = self.controller
        if command == "pause":
            c.pause()
        elif command == "resume":
            c.resume()
        elif command == "toggle":
            c.toggle()
        elif command != "status":
            return "error unknown command"
        return "ok paused" if c.is_paused else "ok running"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), _TIMEOUT_S)
            except (TimeoutError, ValueError):
                return
            sock = writer.get_extra_info("socket")
            uid = peer_uid(sock) if sock is not None else None
            if uid != os.getuid():  # fail closed: an unknown peer is refused too
                log.warning("control socket: rejected connection from uid %s", uid)
                reply = "error forbidden"
            else:
                reply = self.handle_command(line[:_MAX_LINE].decode("ascii", "replace").strip())
            writer.write((reply + "\n").encode("ascii"))
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


def send_command(command: str, path: Path | None = None, timeout: float = _TIMEOUT_S) -> str:
    """Send one command to a running session and return its reply line."""
    if command not in COMMANDS:
        raise ValueError(f"unknown command {command!r}; expected one of {', '.join(COMMANDS)}")
    target = path if path is not None else default_socket_path()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(str(target))
        except OSError as exc:
            raise ControlSocketError(f"no running Sombra session at {target}") from exc
        sock.sendall(command.encode("ascii") + b"\n")
        chunks = []
        while data := sock.recv(256):
            chunks.append(data)
    return b"".join(chunks).decode("ascii", "replace").strip()
