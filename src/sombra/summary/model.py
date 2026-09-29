"""The text-model port used by ``summary`` and its Anthropic Messages adapter.

``summary`` must not import ``brain``, so it declares the one call it needs here:
``complete(system, user, max_tokens) -> (text, Usage)``. The orchestrator (or the
``sombra minutes`` command) passes a concrete adapter; tests pass a fake.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, Protocol

from sombra.contracts import Usage

if TYPE_CHECKING:
    import anthropic

DEFAULT_MODEL = "claude-haiku-4-5"  # cheap and fast; minutes are not answer-latency bound
DEFAULT_CONTEXT_TOKENS = 200_000  # context window of DEFAULT_MODEL


class TextModel(Protocol):
    """A single-turn text completion. Implementations raise on API failure."""

    @property
    def name(self) -> str:
        """Model id reported in ``SummaryEpochLogged.model``."""
        ...

    def complete(self, system: str, user: str, max_tokens: int) -> tuple[str, Usage]: ...


def add_usage(usages: Iterable[Usage]) -> Usage:
    """Sum token counts over several calls."""
    total = Usage()
    for u in usages:
        total = Usage(
            input_tokens=total.input_tokens + u.input_tokens,
            output_tokens=total.output_tokens + u.output_tokens,
            cache_read_input_tokens=total.cache_read_input_tokens + u.cache_read_input_tokens,
            cache_creation_input_tokens=total.cache_creation_input_tokens
            + u.cache_creation_input_tokens,
        )
    return total


class AnthropicTextModel:
    """``TextModel`` over the Anthropic Messages API.

    ``api_key`` is a callable so the key is fetched from the OS keychain at call time
    and never stored in config. ``client_factory`` exists for tests.
    """

    def __init__(
        self,
        api_key: Callable[[], str],
        *,
        model: str = DEFAULT_MODEL,
        timeout_s: float = 120.0,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s
        self._client_factory = client_factory
        self._client: Any = None

    @property
    def name(self) -> str:
        return self._model

    def _get_client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory(self._api_key())
            else:
                self._client = _default_client(self._api_key(), self._timeout_s)
        return self._client

    def complete(self, system: str, user: str, max_tokens: int) -> tuple[str, Usage]:
        response = self._get_client().messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"model {self._model} refused the summary request")
        if response.stop_reason == "max_tokens":
            # A cut-off summary must not reach the prompt prefix, nor half-JSON the parser.
            raise RuntimeError(f"model {self._model} output truncated at max_tokens={max_tokens}")
        text = "".join(b.text for b in response.content if b.type == "text")
        u = response.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_input_tokens=u.cache_read_input_tokens or 0,
            cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
        )
        return text, usage


def _default_client(api_key: str, timeout_s: float) -> anthropic.Anthropic:
    import anthropic  # lazy: keep `import sombra.summary` cheap

    return anthropic.Anthropic(api_key=api_key, timeout=timeout_s)
