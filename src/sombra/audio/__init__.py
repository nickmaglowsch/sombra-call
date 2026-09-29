"""Audio capture: microphone and system output on separate channels (PRD A1, A2).

Separate channels give "EU vs. OUTROS" without diarization. Implements
``contracts.AudioSource``:

- :class:`sombra.audio.macos.MacAudioSource`: live capture on macOS (process tap or
  BlackHole for system audio). Platform bindings load lazily, so this package imports
  on any OS.
- :class:`sombra.audio.file.FileAudioSource`: two WAV files replayed as a source, for
  tests and the replay harness.

Every chunk is 16 kHz mono float32, 20-100 ms long, stamped from one monotonic clock.
Reconnect after device loss (A3) is out of scope here.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from sombra.audio.file import FileAudioSource

if TYPE_CHECKING:
    from sombra.contracts import AudioSource

__all__ = ["FileAudioSource", "default_source"]


def default_source(**kwargs: object) -> AudioSource:
    """The live capture source for this OS; raises on platforms without one yet."""
    if sys.platform == "darwin":
        from sombra.audio.macos import MacAudioSource

        return MacAudioSource(**kwargs)  # type: ignore[arg-type]
    raise NotImplementedError(f"live audio capture is not implemented on {sys.platform}")
