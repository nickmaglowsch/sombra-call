"""ClaudeBrain wired to the real ``brain.prompt`` (#8), with a fake model client."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from sombra.brain import prompt
from sombra.brain.claude import ClaudeBrain, ClaudeSettings, ModelReply, default_prompt_kit
from sombra.contracts import BrainRequest, Channel, SpeechLine, TriggerEvent, Usage

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9"
TS = datetime(2026, 9, 29, 14, 32, 9)


class Client:
    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.requests: list[dict[str, Any]] = []

    async def create(self, request: dict[str, Any], *, timeout_s: float) -> ModelReply:
        self.requests.append(json.loads(json.dumps(request)))
        if request["max_tokens"] == 0:
            return ModelReply([], "max_tokens", Usage())
        text = self.texts.pop(0) if self.texts else "ok"
        return ModelReply([{"type": "text", "text": text}], "end_turn", Usage())

    async def close(self) -> None:
        return None


@pytest.fixture
def meeting(tmp_path: Path) -> Path:
    root = tmp_path / "m"
    (root / "context").mkdir(parents=True)
    (root / "frames").mkdir()
    (root / "context" / "roadmap.md").write_text("# Roadmap\nBeta na sexta.\n", "utf-8")
    (root / "transcript.md").write_text(
        "# Daily\n\n[14:32:07] EU: acho que dá pra fechar na sexta\n", encoding="utf-8"
    )
    for n in (1, 2):
        (root / "frames" / f"f000{n}.jpg").write_bytes(JPEG)
    return root


def trigger(question: str = "Nick, o que você acha desse gráfico?") -> TriggerEvent:
    return TriggerEvent(
        id="t1",
        ts=TS,
        question=question,
        matched_alias="Nick",
        score=0.9,
        window=[SpeechLine(ts=TS, channel=Channel.OTHERS, text=question)],
        needs_screen=True,
    )


def settings() -> ClaudeSettings:
    return ClaudeSettings(user_name="Nick", aliases=("Nicolas",))


def _markers(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, dict):
        found = [node["cache_control"]] if "cache_control" in node else []
        return found + [m for k, v in node.items() if k != "cache_control" for m in _markers(v)]
    if isinstance(node, list):
        return [m for item in node for m in _markers(item)]
    return []


def _prefix(req: dict[str, Any]) -> str:
    """Bytes of system + every user block up to the last breakpoint, markers stripped."""
    blocks = req["messages"][0]["content"]
    last = max(i for i, b in enumerate(blocks) if "cache_control" in b)
    strip = [{k: v for k, v in b.items() if k != "cache_control"} for b in blocks[: last + 1]]
    system = [{k: v for k, v in b.items() if k != "cache_control"} for b in req["system"]]
    return json.dumps([req["tools"], system, strip], ensure_ascii=False)


def test_default_kit_is_brain_prompt() -> None:
    kit = default_prompt_kit()
    assert kit.prefix_builder is prompt.PrefixBuilder
    assert kit.parse_frame_request is prompt.parse_frame_request
    assert ClaudeBrain(settings(), Client())._prompt == kit


async def test_requests_follow_the_cache_rules(meeting: Path) -> None:
    client = Client("primeira", "segunda")
    brain = ClaudeBrain(settings(), client)
    await brain.start(meeting)
    await brain.answer(BrainRequest(trigger(), [meeting / "frames" / "f0001.jpg"]))
    with (meeting / "transcript.md").open("a", encoding="utf-8") as f:
        f.write("[14:33:00] OUTROS: e o prazo?\n")
    await brain.answer(BrainRequest(trigger("Nick, e o prazo?")))

    warm, first, second = client.requests
    for req in client.requests:
        markers = _markers(req)
        assert 1 <= len(markers) <= 4  # API limit
        assert all(m == {"type": "ephemeral", "ttl": "1h"} for m in markers)
        assert "Nick" in req["system"][0]["text"]
    # the warm-up wrote exactly the prefix the first answer reads
    assert _prefix(warm) == _prefix(first)
    # append-only: the second prefix extends the first byte for byte
    assert _prefix(second).startswith(_prefix(first)[:-2])
    assert "e o prazo?" in _prefix(second)
    # the image rode in the first tail only
    assert json.dumps(first).count('"type": "image"') == 1
    assert '"type": "image"' not in json.dumps(second)


async def test_preciso_da_tela_from_the_real_prompt_is_served(meeting: Path) -> None:
    client = Client("PRECISO_DA_TELA f0002", "O gráfico mostra atraso no Q4.")
    brain = ClaudeBrain(settings(), client)
    await brain.start(meeting)
    resp = await brain.answer(BrainRequest(trigger()))
    assert resp.text == "O gráfico mostra atraso no Q4."
    assert list(resp.frames_sent) == ["f0002"]
    served = client.requests[-1]["messages"][-1]["content"]
    assert any(b["type"] == "image" for b in served)
