"""Trigger detection: notices when someone in the call asks the user something.

Owns PRD G1-G5: fuzzy name match on ``OUTROS`` lines (G1), request structure and
vocative position (G2), the 60 s context window (G3), the deictic screen heuristic and
candidate frames (G4), cooldown and dedupe (G5). Implements
``sombra.contracts.TriggerDetector`` as :class:`NameTriggerDetector`. The phase-3
classifier (G6) is out of scope here.
"""

from sombra.trigger.detector import NameTriggerDetector, TriggerSettings
from sombra.trigger.evaluate import CorpusCase, EvalResult, evaluate, load_corpus

__all__ = [
    "CorpusCase",
    "EvalResult",
    "NameTriggerDetector",
    "TriggerSettings",
    "evaluate",
    "load_corpus",
]
