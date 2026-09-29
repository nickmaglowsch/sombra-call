"""Shared fakes of every port in ``sombra.contracts.ports``.

Faithful to the contract docstrings, deterministic, and free of devices, models
and network. Any module's tests may reuse them: ``from fakes import FakeBrain``
(``tests/`` is on ``pythonpath``, see ``pyproject.toml``).
"""

from fakes.audio import FakeAudioSource
from fakes.brain import FakeBrain
from fakes.screen import FakeFramePipeline, FakeScreenSource
from fakes.store import InMemoryStore
from fakes.transcription import FakeTranscriber
from fakes.trigger import ScriptedTriggerDetector, TriggerRule
from fakes.ui import FakeUI

__all__ = [
    "FakeAudioSource",
    "FakeBrain",
    "FakeFramePipeline",
    "FakeScreenSource",
    "FakeTranscriber",
    "FakeUI",
    "InMemoryStore",
    "ScriptedTriggerDetector",
    "TriggerRule",
]
