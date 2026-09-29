"""Runtime settings for one :class:`~sombra.orchestrator.session.Session`."""

from __future__ import annotations

from dataclasses import dataclass

from sombra.contracts import AutonomyLevel

MAX_FRAMES_PER_CALL = 3  # images in one brain call's ephemeral tail (C6)


@dataclass(frozen=True, slots=True)
class SessionSettings:
    autonomy: AutonomyLevel = AutonomyLevel.L2
    screen_interval_s: float = 5.0  # PRD: one screenshot every 5-10 s
    brain_timeout_s: float = 20.0  # past this the user answers manually
    epoch_interval_s: float = 10 * 60.0  # rolling summary cadence (C4)
    queue_size: int = 256  # bound of the audio and timeline queues
    max_frames: int = MAX_FRAMES_PER_CALL
    restart_backoff_s: float = 0.5  # first restart delay of a crashed pipeline task
    restart_backoff_max_s: float = 30.0
    drain_timeout_s: float = 30.0  # stop(): how long to wait for queues to drain
    ui_timeout_s: float = 5.0  # a hung overlay call must not wedge the trigger task
    minutes_timeout_s: float = 300.0  # stop(): end-of-meeting minutes (an LLM call)
    close_timeout_s: float = 5.0  # stop(): each port's close()

    def __post_init__(self) -> None:
        positive = {
            "screen_interval_s": self.screen_interval_s,
            "brain_timeout_s": self.brain_timeout_s,
            "epoch_interval_s": self.epoch_interval_s,
            "queue_size": self.queue_size,
            "restart_backoff_s": self.restart_backoff_s,
            "restart_backoff_max_s": self.restart_backoff_max_s,
            "drain_timeout_s": self.drain_timeout_s,
            "ui_timeout_s": self.ui_timeout_s,
            "minutes_timeout_s": self.minutes_timeout_s,
            "close_timeout_s": self.close_timeout_s,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be > 0, got {value}")
        if not 0 <= self.max_frames <= MAX_FRAMES_PER_CALL:
            raise ValueError(f"max_frames must be in 0..{MAX_FRAMES_PER_CALL}")
        if self.restart_backoff_max_s < self.restart_backoff_s:
            raise ValueError("restart_backoff_max_s must be >= restart_backoff_s")
