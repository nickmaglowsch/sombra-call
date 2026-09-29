"""brain.claude.AnthropicClient: SDK calls and error mapping, with the SDK faked."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

import anthropic
import httpx2
import pytest

from sombra.brain.claude import (
    REFUSAL_FALLBACK_BETA,
    AnthropicClient,
    BrainAPIError,
    BrainAuthError,
    BrainOverloadedError,
    BrainRateLimitError,
    BrainTimeoutError,
)
from sombra.contracts import Usage

SECRET = "sk-ant-test-not-a-real-key"  # noqa: S105 - fake key for the redaction test
REQUEST = {"model": "claude-opus-5-5", "max_tokens": 64, "messages": []}


class FakeMessage:
    def to_dict(self) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": "oi"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 3,
                "output_tokens": 4,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": None,
            },
        }


class FakeMessages:
    def __init__(self, sdk: FakeSdk) -> None:
        self.sdk = sdk

    async def create(self, **kwargs: Any) -> FakeMessage:
        self.sdk.calls.append(kwargs)
        if self.sdk.error is not None:
            raise self.sdk.error
        return FakeMessage()


class FakeSdk:
    instances: ClassVar[list[FakeSdk]] = []

    def __init__(self, *, api_key: str, max_retries: int) -> None:
        self.api_key = api_key
        self.max_retries = max_retries
        self.calls: list[dict[str, Any]] = []
        self.timeouts: list[float] = []
        self.error: BaseException | None = None
        self.messages = FakeMessages(self)
        self.beta = self
        self.closed = False
        FakeSdk.instances.append(self)

    def with_options(self, *, timeout: float) -> FakeSdk:
        self.timeouts.append(timeout)
        return self

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSdk.instances = []
    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeSdk)


def status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(code, request=req), body=None)


async def test_create_uses_fallback_beta_and_maps_the_reply() -> None:
    keys: list[int] = []

    def key() -> str:
        keys.append(1)
        return SECRET

    client = AnthropicClient(key)
    reply = await client.create(REQUEST, timeout_s=7.5)
    await client.create(REQUEST, timeout_s=3.0)
    sdk = FakeSdk.instances[0]
    assert len(FakeSdk.instances) == 1 and len(keys) == 1  # key fetched once, lazily
    assert sdk.api_key == SECRET and sdk.max_retries == 0
    assert sdk.timeouts == [7.5, 3.0]
    assert sdk.calls[0]["betas"] == [REFUSAL_FALLBACK_BETA]
    assert sdk.calls[0]["fallbacks"] == "default"
    assert reply.content == [{"type": "text", "text": "oi"}]
    assert reply.stop_reason == "end_turn"
    assert reply.usage == Usage(3, 4, 5, 0)
    await client.close()
    assert sdk.closed
    await client.close()  # idempotent


async def test_create_without_fallback_uses_plain_messages() -> None:
    client = AnthropicClient(lambda: SECRET, refusal_fallback=False)
    await client.create(REQUEST, timeout_s=1.0)
    assert "betas" not in FakeSdk.instances[0].calls[0]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (status_error(anthropic.OverloadedError, 529), BrainOverloadedError),
        (status_error(anthropic.ServiceUnavailableError, 503), BrainOverloadedError),
        (status_error(anthropic.RateLimitError, 429), BrainRateLimitError),
        (status_error(anthropic.AuthenticationError, 401), BrainAuthError),
        (status_error(anthropic.PermissionDeniedError, 403), BrainAuthError),
        (status_error(anthropic.BadRequestError, 400), BrainAPIError),
        (status_error(anthropic.InternalServerError, 500), BrainAPIError),
        (
            anthropic.APITimeoutError(httpx2.Request("POST", "https://api.anthropic.com")),
            BrainTimeoutError,
        ),
        (
            anthropic.APIConnectionError(
                request=httpx2.Request("POST", "https://api.anthropic.com")
            ),
            BrainAPIError,
        ),
    ],
)
async def test_sdk_errors_become_typed_brain_errors(
    error: BaseException, expected: type[Exception], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client = AnthropicClient(lambda: SECRET)
    await client.create(REQUEST, timeout_s=1.0)  # build the SDK client
    FakeSdk.instances[0].error = error
    with pytest.raises(expected) as info:
        await client.create(REQUEST, timeout_s=1.0)
    assert SECRET not in str(info.value)
    assert SECRET not in caplog.text
