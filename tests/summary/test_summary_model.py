from dataclasses import dataclass, field
from typing import Any

import pytest

from sombra.contracts import Usage
from sombra.summary.model import DEFAULT_MODEL, AnthropicTextModel, add_usage


@dataclass
class _Block:
    type: str
    text: str = ""


@dataclass
class _Usage:
    input_tokens: int = 100
    output_tokens: int = 20
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = 5


@dataclass
class _Response:
    content: list[_Block]
    stop_reason: str = "end_turn"
    usage: _Usage = field(default_factory=_Usage)


class _Messages:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> _Response:
        self.kwargs = kwargs
        return self.response


class _Client:
    def __init__(self, response: _Response) -> None:
        self.messages = _Messages(response)


def test_complete_maps_request_and_usage() -> None:
    keys: list[str] = []
    client = _Client(
        _Response([_Block("thinking"), _Block("text", "olá "), _Block("text", "mundo")])
    )

    def factory(key: str) -> _Client:
        keys.append(key)
        return client

    model = AnthropicTextModel(lambda: "sk-test", model="claude-x", client_factory=factory)
    assert model.name == "claude-x"
    text, usage = model.complete("sys", "user", 123)
    model.complete("sys", "user", 123)
    assert text == "olá mundo"
    assert usage == Usage(100, 20, 0, 5)
    assert keys == ["sk-test"]  # key fetched lazily, once
    assert client.messages.kwargs == {
        "model": "claude-x",
        "max_tokens": 123,
        "system": "sys",
        "messages": [{"role": "user", "content": "user"}],
    }


def test_refusal_raises() -> None:
    client = _Client(_Response([], stop_reason="refusal"))
    model = AnthropicTextModel(lambda: "k", client_factory=lambda k: client)
    with pytest.raises(RuntimeError, match="refused"):
        model.complete("s", "u", 10)


def test_default_model_and_real_client_construction() -> None:
    model = AnthropicTextModel(lambda: "sk-not-used")
    assert model.name == DEFAULT_MODEL
    client = model._get_client()  # builds the real SDK client; no request is made
    assert type(client).__name__ == "Anthropic"


def test_add_usage() -> None:
    assert add_usage([Usage(1, 2, 3, 4), Usage(10, 20, 30, 40)]) == Usage(11, 22, 33, 44)
    assert add_usage([]) == Usage()


def test_truncated_output_raises() -> None:
    client = _Client(_Response([_Block("text", '{"resumo": "cort')], stop_reason="max_tokens"))
    model = AnthropicTextModel(lambda: "k", client_factory=lambda k: client)
    with pytest.raises(RuntimeError, match="truncated"):
        model.complete("s", "u", 10)
