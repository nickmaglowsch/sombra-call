"""Loopback HTTP + websocket server behind the overlay page.

- Binds ``127.0.0.1`` (or ``::1``) only, on a random free port.
- Every request must carry the session token (``?token=`` or ``X-Sombra-Token``);
  anything else gets 403. The ``Host`` header must name the loopback address too,
  which blocks DNS-rebinding pages from talking to the server.
- ``GET /``            the overlay page (HTML with its CSS/JS inlined under a CSP nonce)
- ``GET /ws``          websocket: server pushes state, client sends actions
- ``GET /frames/<id>`` a JPEG from the meeting's ``frames/`` dir (thumbnail of a frame sent)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import socket
from collections.abc import Awaitable, Callable
from importlib import resources
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from sombra.ui.state import MAX_MESSAGE_BYTES

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_FRAME_ID_RE = re.compile(r"^f\d{4,}$")

Snapshot = Callable[[], dict[str, Any]]
MessageHandler = Callable[[str], Awaitable[str | None]]
"""Handles one client message; returns an error text to send back, or None."""


def _asset(name: str) -> str:
    return resources.files("sombra.ui").joinpath("static", name).read_text(encoding="utf-8")


def render_page(nonce: str) -> str:
    """The overlay page with CSS and JS inlined, so one token-checked request loads it."""
    return (
        _asset("index.html")
        .replace("%NONCE%", nonce)
        .replace("/*%STYLE%*/", _asset("style.css"))
        .replace("/*%SCRIPT%*/", _asset("app.js"))
    )


class OverlayServer:
    def __init__(
        self,
        *,
        snapshot: Snapshot,
        on_message: MessageHandler,
        frames_dir: Path | None = None,
        token: str | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        if host not in LOOPBACK_HOSTS:
            raise ValueError(f"overlay server must bind a loopback address, not {host!r}")
        self._snapshot = snapshot
        self._on_message = on_message
        self._frames_dir = frames_dir
        self.token = token or secrets.token_urlsafe(32)
        self.host = host
        self._requested_port = port
        self._port: int | None = None
        self._runner: web.AppRunner | None = None
        self._clients: set[web.WebSocketResponse] = set()

    # --- lifecycle -----------------------------------------------------------------

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("server not started")
        return self._port

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def url(self) -> str:
        """Page URL including the token: what the window opens."""
        return f"{self.base_url}/?token={self.token}"

    async def start(self) -> None:
        app = web.Application(middlewares=[self._guard])
        app.router.add_get("/", self._page)
        app.router.add_get("/ws", self._ws)
        app.router.add_get("/frames/{frame_id}", self._frame)
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.bind((self.host, self._requested_port))
        self._port = sock.getsockname()[1]
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.SockSite(self._runner, sock).start()

    async def close(self) -> None:
        for ws in list(self._clients):
            await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # --- push ------------------------------------------------------------------------

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        clients = [ws for ws in self._clients if not ws.closed]
        results = await asyncio.gather(
            *(ws.send_str(data) for ws in clients), return_exceptions=True
        )
        for ws, result in zip(clients, results, strict=True):
            if isinstance(result, Exception):
                log.debug("dropping overlay client: %s", result)
                self._clients.discard(ws)

    # --- request handling --------------------------------------------------------------

    def _allowed_hosts(self) -> set[str]:
        literal = "[::1]" if self.host == "::1" else self.host
        return {f"{literal}:{self._port}", f"localhost:{self._port}"}

    @web.middleware
    async def _guard(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        if request.host not in self._allowed_hosts():
            raise web.HTTPForbidden(text="bad host")
        given = request.query.get("token") or request.headers.get("X-Sombra-Token") or ""
        if not secrets.compare_digest(given.encode(), self.token.encode()):
            raise web.HTTPForbidden(text="bad token")
        origin = request.headers.get("Origin")
        if origin is not None and origin.removeprefix("http://") not in self._allowed_hosts():
            raise web.HTTPForbidden(text="bad origin")
        return await handler(request)

    async def _page(self, request: web.Request) -> web.Response:
        nonce = secrets.token_urlsafe(16)
        csp = (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            f"img-src 'self'; connect-src ws://{request.host}; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        return web.Response(
            text=render_page(nonce),
            content_type="text/html",
            headers={
                "Content-Security-Policy": csp,
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

    async def _frame(self, request: web.Request) -> web.FileResponse:
        frame_id = request.match_info["frame_id"].removesuffix(".jpg")
        if self._frames_dir is None or not _FRAME_ID_RE.match(frame_id):
            raise web.HTTPNotFound()
        path = self._frames_dir / f"{frame_id}.jpg"
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=MAX_MESSAGE_BYTES)
        await ws.prepare(request)
        self._clients.add(ws)
        try:
            await ws.send_str(json.dumps(self._snapshot(), ensure_ascii=False))
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    continue
                error = await self._on_message(msg.data)
                if error is not None:
                    with contextlib.suppress(ConnectionError):
                        await ws.send_str(json.dumps({"type": "error", "message": error}))
        finally:
            self._clients.discard(ws)
        return ws
