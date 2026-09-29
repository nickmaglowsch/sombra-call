"""Loopback server hardening: token, Host and Origin checks, page, frames."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from sombra.ui.server import OverlayServer, render_page


async def _noop(raw: str) -> str | None:
    return None


def _snapshot() -> dict[str, Any]:
    return {"type": "state", "cards": []}


@pytest.fixture
async def server(tmp_path: Path) -> AsyncIterator[OverlayServer]:
    (tmp_path / "f0007.jpg").write_bytes(b"\xff\xd8jpeg")
    (tmp_path / "secret.txt").write_text("nope")
    srv = OverlayServer(snapshot=_snapshot, on_message=_noop, frames_dir=tmp_path)
    await srv.start()
    yield srv
    await srv.close()


@pytest.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


async def test_page_needs_token(server: OverlayServer, http: aiohttp.ClientSession) -> None:
    async with http.get(f"{server.base_url}/") as r:
        assert r.status == 403
    async with http.get(f"{server.base_url}/?token=wrong") as r:
        assert r.status == 403
    async with http.get(server.url) as r:
        assert r.status == 200
        body = await r.text()
        csp = r.headers["Content-Security-Policy"]
    assert "%NONCE%" not in body
    assert "/*%SCRIPT%*/" not in body
    assert "new WebSocket" in body
    assert "default-src 'none'" in csp
    assert f"connect-src ws://127.0.0.1:{server.port}" in csp
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in body


async def test_token_header_is_accepted(server: OverlayServer, http: aiohttp.ClientSession) -> None:
    async with http.get(f"{server.base_url}/", headers={"X-Sombra-Token": server.token}) as r:
        assert r.status == 200


async def test_websocket_needs_token(server: OverlayServer, http: aiohttp.ClientSession) -> None:
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await http.ws_connect(f"{server.base_url}/ws")
    assert err.value.status == 403
    with pytest.raises(aiohttp.WSServerHandshakeError):
        await http.ws_connect(f"{server.base_url}/ws?token=x{server.token}")
    ws = await http.ws_connect(f"{server.base_url}/ws?token={server.token}")
    assert await ws.receive_json(timeout=2) == _snapshot()
    await ws.close()


async def test_foreign_host_header_is_rejected(
    server: OverlayServer, http: aiohttp.ClientSession
) -> None:
    """DNS rebinding: a page on evil.example resolving to 127.0.0.1 still sends its Host."""
    async with http.get(server.url, headers={"Host": f"evil.example:{server.port}"}) as r:
        assert r.status == 403
    async with http.get(server.url, headers={"Host": f"localhost:{server.port}"}) as r:
        assert r.status == 200


async def test_foreign_origin_is_rejected(
    server: OverlayServer, http: aiohttp.ClientSession
) -> None:
    ws_url = f"{server.base_url}/ws?token={server.token}"
    with pytest.raises(aiohttp.WSServerHandshakeError):
        await http.ws_connect(ws_url, origin="http://evil.example")
    ws = await http.ws_connect(ws_url, origin=server.base_url)
    await ws.close()


async def test_frames_route(server: OverlayServer, http: aiohttp.ClientSession) -> None:
    q = f"?token={server.token}"
    async with http.get(f"{server.base_url}/frames/f0007{q}") as r:
        assert r.status == 200
        assert await r.read() == b"\xff\xd8jpeg"
    async with http.get(f"{server.base_url}/frames/f0007.jpg{q}") as r:
        assert r.status == 200
    for bad in ("f0008", "secret.txt", "..%2Fsecret.txt", "f12", "f0007%0A"):
        async with http.get(f"{server.base_url}/frames/{bad}{q}") as r:
            assert r.status == 404, bad
    async with http.get(f"{server.base_url}/frames/f0007") as r:
        assert r.status == 403


async def test_frames_without_dir(http: aiohttp.ClientSession) -> None:
    srv = OverlayServer(snapshot=_snapshot, on_message=_noop)
    await srv.start()
    try:
        async with http.get(f"{srv.base_url}/frames/f0001?token={srv.token}") as r:
            assert r.status == 404
    finally:
        await srv.close()


async def test_ipv6_loopback(http: aiohttp.ClientSession) -> None:
    srv = OverlayServer(snapshot=_snapshot, on_message=_noop, host="::1")
    try:
        await srv.start()
    except OSError:
        pytest.skip("no IPv6 loopback on this machine")
    try:
        assert srv.base_url.startswith("http://[::1]:")
        async with http.get(srv.url) as r:
            assert r.status == 200
    finally:
        await srv.close()


async def test_error_reply_and_dead_client_is_dropped(http: aiohttp.ClientSession) -> None:
    async def reject(raw: str) -> str | None:
        return "nope"

    srv = OverlayServer(snapshot=_snapshot, on_message=reject)
    await srv.start()
    try:
        ws = await http.ws_connect(f"{srv.base_url}/ws?token={srv.token}")
        await ws.receive_json(timeout=2)
        await ws.send_str("{}")
        assert await ws.receive_json(timeout=2) == {"type": "error", "message": "nope"}
        await ws.close()
        await srv.broadcast({"type": "state", "cards": []})
        assert srv.client_count == 0
    finally:
        await srv.close()


def test_shortcuts_ignore_key_repeat_and_new_top_card() -> None:
    """Holding a key must not resolve a queue of unread suggestions (review of #31)."""
    page = render_page("n0nce")
    assert "if (ev.repeat) return;" in page
    assert "performance.now() < armedAt" in page


def test_page_escapes_nothing_from_meeting() -> None:
    """Meeting text reaches the page only as JSON data, rendered with textContent."""
    page = render_page("n0nce")
    assert "innerHTML" not in page
    assert "insertAdjacentHTML" not in page
    assert "textContent" in page
