"""OverlayUI end to end over a real loopback websocket (no window)."""

import asyncio
import json
import statistics
import time
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from sombra.contracts import ActionKind, ApprovalUI, Suggestion, TriggerEvent, UserAction
from sombra.ui import OverlayUI


class Recorder:
    def __init__(self) -> None:
        self.notes: list[tuple[str, str]] = []
        self.copied: list[str] = []

    async def notify(self, title: str, body: str) -> None:
        self.notes.append((title, body))

    async def copy(self, text: str) -> None:
        self.copied.append(text)


def _trigger(n: int) -> TriggerEvent:
    return TriggerEvent(
        id=f"t{n}",
        ts=datetime(2026, 9, 29, 14, 32, 9),
        question=f"Nick, o que você acha disso {n}?",
        matched_alias="Nick",
        score=0.9,
        window=(),
        needs_screen=False,
    )


def _suggestion(n: int, frames: tuple[str, ...] = ()) -> Suggestion:
    return Suggestion(
        id=f"s{n}",
        trigger_id=f"t{n}",
        text=f"resposta {n}",
        excerpt=f"Nick, o que você acha disso {n}?",
        frames_sent=frames,
    )


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
async def ui(rec: Recorder, tmp_path: Path) -> AsyncIterator[OverlayUI]:
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "f0001.jpg").write_bytes(b"\xff\xd8fake-jpeg")
    overlay = OverlayUI(frames_dir=frames, notifier=rec.notify, clipboard=rec.copy)
    await overlay.start()
    yield overlay
    await overlay.close()


@pytest.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


async def _connect(ui: OverlayUI, http: aiohttp.ClientSession) -> aiohttp.ClientWebSocketResponse:
    ws = await http.ws_connect(f"{ui.server.base_url}/ws?token={ui.token}")
    first = await ws.receive_json(timeout=2)
    assert first == {"type": "state", "cards": []}
    return ws


async def _next(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
    msg: dict[str, Any] = await ws.receive_json(timeout=2)
    return msg


async def _drain_until(ui: OverlayUI) -> None:
    for _ in range(100):
        if not ui._background:
            return
        await asyncio.sleep(0.01)


def test_implements_contract() -> None:
    assert isinstance(OverlayUI(), ApprovalUI)


def test_server_binds_loopback_only() -> None:
    with pytest.raises(ValueError, match="loopback"):
        OverlayUI(host="0.0.0.0")  # noqa: S104 - asserting it is refused


async def test_url_is_loopback_with_token(ui: OverlayUI) -> None:
    assert ui.url.startswith("http://127.0.0.1:")
    assert ui.url.endswith(f"/?token={ui.token}")
    assert len(ui.token) >= 32


async def test_protocol_round_trip(
    ui: OverlayUI, http: aiohttp.ClientSession, rec: Recorder
) -> None:
    ws = await _connect(ui, http)

    await ui.notify_trigger(_trigger(1))
    state = await _next(ws)
    [card] = state["cards"]
    assert card["kind"] == "searching"
    assert card["excerpt"] == "Nick, o que você acha disso 1?"

    await ui.show(_suggestion(1, frames=("f0001",)))
    [card] = (await _next(ws))["cards"]
    assert card["kind"] == "suggestion"
    assert card["text"] == "resposta 1"
    assert card["frames"] == ["f0001"]

    await ws.send_json({"type": "action", "suggestion_id": "s1", "kind": "approve"})
    assert (await _next(ws))["cards"] == []
    actions = ui.actions()
    assert await anext(actions) == UserAction("s1", ActionKind.APPROVE, "resposta 1")
    await _drain_until(ui)
    assert rec.copied == ["resposta 1"]
    assert rec.notes == [("Sombra: buscando contexto…", "Nick, o que você acha disso 1?")]
    await ws.close()


@pytest.mark.parametrize(
    ("msg", "expected", "copied"),
    [
        ({"kind": "approve"}, UserAction("s1", ActionKind.APPROVE, "resposta 1"), ["resposta 1"]),
        (
            {"kind": "edit", "text": "editada"},
            UserAction("s1", ActionKind.EDIT, "editada"),
            ["editada"],
        ),
        ({"kind": "discard"}, UserAction("s1", ActionKind.DISCARD), []),
        ({"kind": "not_for_me"}, UserAction("s1", ActionKind.NOT_FOR_ME), []),
    ],
)
async def test_each_button_over_the_wire(
    ui: OverlayUI,
    http: aiohttp.ClientSession,
    rec: Recorder,
    msg: dict[str, str],
    expected: UserAction,
    copied: list[str],
) -> None:
    ws = await _connect(ui, http)
    await ui.show(_suggestion(1))
    await _next(ws)
    await ws.send_json({"type": "action", "suggestion_id": "s1", **msg})
    await _next(ws)
    assert await anext(ui.actions()) == expected
    await _drain_until(ui)
    assert rec.copied == copied
    await ws.close()


async def test_no_clipboard_when_copy_disabled(rec: Recorder, http: aiohttp.ClientSession) -> None:
    async with OverlayUI(copy_on_approve=False, notifier=rec.notify, clipboard=rec.copy) as ui:
        ws = await _connect(ui, http)
        await ui.show(_suggestion(1))
        await _next(ws)
        await ws.send_json({"type": "action", "suggestion_id": "s1", "kind": "approve"})
        assert (await anext(ui.actions())).kind is ActionKind.APPROVE
        assert rec.copied == []
        await ws.close()


async def test_queue_newest_on_top_and_actions_in_user_order(
    ui: OverlayUI, http: aiohttp.ClientSession
) -> None:
    ws = await _connect(ui, http)
    for n in (1, 2, 3):
        await ui.show(_suggestion(n))
        state = await _next(ws)
    assert [c["suggestion_id"] for c in state["cards"]] == ["s3", "s2", "s1"]

    for sid in ("s2", "s3", "s1"):
        await ws.send_json({"type": "action", "suggestion_id": sid, "kind": "discard"})
        await _next(ws)
    got = []
    async for action in ui.actions():
        got.append(action.suggestion_id)
        if len(got) == 3:
            break
    assert got == ["s2", "s3", "s1"]
    await ws.close()


async def test_invalid_message_gets_error_and_no_action(
    ui: OverlayUI, http: aiohttp.ClientSession
) -> None:
    ws = await _connect(ui, http)
    await ws.send_json({"type": "action", "suggestion_id": "ghost", "kind": "approve"})
    reply = await _next(ws)
    assert reply["type"] == "error"
    assert "not pending" in reply["message"]
    assert ui._actions.empty()
    await ws.send_bytes(b"binary is ignored")
    await ws.send_str("not json")
    assert (await _next(ws))["type"] == "error"
    await ws.close()


async def test_failure_notification(
    ui: OverlayUI, http: aiohttp.ClientSession, rec: Recorder
) -> None:
    ws = await _connect(ui, http)
    await ui.notify_trigger(_trigger(1))
    await _next(ws)
    await ui.notify_failure(_trigger(1), "timeout do agente")
    [card] = (await _next(ws))["cards"]
    assert card["kind"] == "failure"
    assert card["reason"] == "timeout do agente"
    assert card["excerpt"] == "Nick, o que você acha disso 1?"
    await _drain_until(ui)
    assert rec.notes[-1] == ("Sombra: responda manualmente", "Nick, o que você acha disso 1?")

    await ws.send_json({"type": "dismiss", "card_id": card["id"]})
    assert (await _next(ws))["cards"] == []
    await ws.close()


async def test_every_client_gets_the_push(ui: OverlayUI, http: aiohttp.ClientSession) -> None:
    a, b = await _connect(ui, http), await _connect(ui, http)
    assert ui.server.client_count == 2
    await ui.show(_suggestion(1))
    assert (await _next(a))["cards"][0]["id"] == "s:s1"
    assert (await _next(b))["cards"][0]["id"] == "s:s1"
    await a.send_json({"type": "action", "suggestion_id": "s1", "kind": "discard"})
    assert (await _next(b))["cards"] == []
    await a.close()
    await b.close()


async def test_show_reaches_client_under_100_ms(ui: OverlayUI, http: aiohttp.ClientSession) -> None:
    """Acceptance: the suggestion is on the wire < 100 ms after show() (median of 20)."""
    ws = await _connect(ui, http)
    samples = []
    for n in range(20):
        start = time.perf_counter()
        await ui.show(_suggestion(n))
        await _next(ws)
        samples.append((time.perf_counter() - start) * 1000)
    assert statistics.median(samples) < 100, samples
    assert ui.last_show_ms is not None
    await ws.close()


async def test_side_effect_errors_do_not_break_the_overlay(http: aiohttp.ClientSession) -> None:
    async def boom(*_: str) -> None:
        raise RuntimeError("no notification center")

    async with OverlayUI(notifier=boom, clipboard=boom) as ui:
        ws = await _connect(ui, http)
        await ui.notify_trigger(_trigger(1))
        await _next(ws)
        await ui.show(_suggestion(1))
        await _next(ws)
        await ws.send_json({"type": "action", "suggestion_id": "s1", "kind": "approve"})
        assert (await anext(ui.actions())).kind is ActionKind.APPROVE
        await _drain_until(ui)
        await ws.close()


async def test_actions_end_on_close(rec: Recorder) -> None:
    ui = OverlayUI(notifier=rec.notify, clipboard=rec.copy)
    await ui.start()
    collected: list[UserAction] = []

    async def consume() -> None:
        async for a in ui.actions():
            collected.append(a)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await ui.close()
    await asyncio.wait_for(task, timeout=2)
    assert collected == []


async def test_close_drops_connected_clients(rec: Recorder, http: aiohttp.ClientSession) -> None:
    ui = OverlayUI(notifier=rec.notify, clipboard=rec.copy)
    await ui.start()
    ws = await _connect(ui, http)
    await ui.close()
    msg = await ws.receive(timeout=2)
    assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)


def test_state_is_exposed() -> None:
    ui = OverlayUI()
    assert ui.state.cards() == []
    with pytest.raises(RuntimeError, match="not started"):
        _ = ui.url


async def test_snapshot_json_is_utf8(ui: OverlayUI, http: aiohttp.ClientSession) -> None:
    ws = await _connect(ui, http)
    await ui.show(_suggestion(1))
    raw = await ws.receive_str(timeout=2)
    assert "você" in raw
    assert json.loads(raw)["cards"][0]["excerpt"].startswith("Nick")
    await ws.close()
