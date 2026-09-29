"""Rolling summary epochs and end-of-meeting minutes (PRD C4, M3; autonomy level L0).

Owns ``summary.md`` in the meeting folder (layout in ``summary_file``), the
``TextModel`` port this package calls (``model``) with its Anthropic API and
subscription-CLI adapters (``cli_models``), ``EpochSummarizer`` (``epochs``),
and ``write_minutes`` (``minutes``). Deciding *when* to run an epoch or the minutes, which
model to use, and the ``sombra minutes`` command are the orchestrator's job (it reads the
user config, which this package must not).
"""

from sombra.summary.cli_models import ClaudeCliTextModel, CodexCliTextModel
from sombra.summary.epochs import EpochResult, EpochSummarizer
from sombra.summary.minutes import (
    ActionItem,
    Minutes,
    MinutesParseError,
    MinutesResult,
    generate_minutes,
    parse_minutes,
    write_minutes,
)
from sombra.summary.model import DEFAULT_MODEL, AnthropicTextModel, TextModel
from sombra.summary.summary_file import (
    EpochSection,
    append_epoch,
    read_current_epoch,
    read_epochs,
    read_minutes_section,
)

__all__ = [
    "DEFAULT_MODEL",
    "ActionItem",
    "AnthropicTextModel",
    "ClaudeCliTextModel",
    "CodexCliTextModel",
    "EpochResult",
    "EpochSection",
    "EpochSummarizer",
    "Minutes",
    "MinutesParseError",
    "MinutesResult",
    "TextModel",
    "append_epoch",
    "generate_minutes",
    "parse_minutes",
    "read_current_epoch",
    "read_epochs",
    "read_minutes_section",
    "write_minutes",
]
