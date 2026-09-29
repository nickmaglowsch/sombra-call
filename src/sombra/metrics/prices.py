"""API price table, loaded from a small TOML file (prices are never hard-coded).

Format, in US$ per million tokens::

    [models."claude-sonnet-5-5"]
    input = 3.0
    output = 15.0
    cache_read = 0.3
    cache_creation = 3.75

    [models."*"]          # optional fallback for models not listed
    input = 3.0
    ...

Every key is optional and defaults to 0. A model missing from the table (with no
``"*"`` entry) makes the cost unknown rather than silently zero.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sombra.contracts import Usage

FALLBACK = "*"
_KEYS = ("input", "output", "cache_read", "cache_creation")


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """US$ per million tokens."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_creation: float = 0.0

    def cost(self, usage: Usage) -> float:
        return (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cache_read_input_tokens * self.cache_read
            + usage.cache_creation_input_tokens * self.cache_creation
        ) / 1_000_000


@dataclass(frozen=True, slots=True)
class PriceTable:
    models: dict[str, ModelPrice]

    def price_for(self, model: str) -> ModelPrice | None:
        return self.models.get(model, self.models.get(FALLBACK))


def parse_price_table(text: str) -> PriceTable:
    data = tomllib.loads(text)
    models = data.get("models", {})
    if not isinstance(models, dict):
        raise ValueError("price table: [models] must be a table")
    return PriceTable({name: _model_price(name, entry) for name, entry in models.items()})


def load_price_table(path: Path) -> PriceTable:
    return parse_price_table(path.read_text(encoding="utf-8"))


def _model_price(name: str, entry: Any) -> ModelPrice:
    if not isinstance(entry, dict):
        raise ValueError(f"price table: models.{name} must be a table")
    values: dict[str, float] = {}
    for key, value in entry.items():
        if key not in _KEYS:
            raise ValueError(f"price table: unknown key models.{name}.{key}")
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise ValueError(f"price table: models.{name}.{key} must be a number >= 0")
        values[key] = float(value)
    return ModelPrice(**values)
