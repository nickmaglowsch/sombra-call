"""The wiring helpers: config translation, hook adapters, summary hooks and ``run_live``."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_orchestrator_replay import FIXTURE, fake_transcriber

from sombra.brain.claude import ClaudeBrain
from sombra.config import UserConfig, UserIdentity
from sombra.config.schema import BrainConfig
from sombra.contracts import (
    AutonomyLevel,
    BrainRequest,
    Channel,
    FrameMarker,
    FrameRecord,
    SpeechLine,
    Suggestion,
    TriggerEvent,
    TriggerLogged,
    Usage,
)
from sombra.orchestrator.live import (
    LivePlan,
    StopSignal,
    build_brain,
    discard_meeting,
    run_live,
    summary_model,
)
from sombra.orchestrator.replay import ReplayScreen
from sombra.orchestrator.wiring import (
    Assembly,
    NoBrain,
    NoTrigger,
    OneMarkerStore,
    PauseAdapter,
    SilentUI,
    SummaryHooks,
    agent_model,
    build_agent_brain,
    build_detector,
    build_transcriber,
    key_provider,
    resolve_claude_model,
    resolve_whisper_model,
)
from sombra.privacy import PauseController
from sombra.screen.replay import DirectoryScreenSource
from sombra.store import MeetingStore, create_meeting
from sombra.summary import AnthropicTextModel, read_epochs, read_minutes_section
from sombra.trigger import NameTriggerDetector

T0 = datetime(2026, 9, 29, 14, 30, 0).astimezone()


def test_whisper_model_names_from_the_config() -> None:
    assert resolve_whisper_model("tiny") == "tiny"
    assert resolve_whisper_model("large-v3-turbo") == "large-v3-turbo-q5_0"  # config default
    assert resolve_whisper_model("small-q5_1") == "small-q5_1"
    with pytest.raises(ValueError, match=r"models\.stt"):
        resolve_whisper_model("huge")


def test_claude_model_aliases() -> None:
    assert resolve_claude_model("sonnet") == "claude-sonnet-5-5"
    assert resolve_claude_model(" Haiku ") == "claude-haiku-4-5"
    assert resolve_claude_model("claude-opus-5-5") == "claude-opus-5-5"


def test_pause_adapter_reads_the_controller() -> None:
    controller = PauseController()
    adapter = PauseAdapter(controller)
    assert adapter.is_paused() is False
    controller.pause()
    assert adapter.is_paused() is True


def test_build_transcriber_checks_the_models(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"download_models\.py tiny"):
        build_transcriber("tiny", models_dir=tmp_path)
    for name in ("ggml-tiny.bin", "silero_vad.onnx"):
        (tmp_path / name).write_bytes(b"")
    assert build_transcriber("tiny", vocabulary=["Maria"], models_dir=tmp_path) is not None


def test_detector_needs_a_name_else_never_fires() -> None:
    assert isinstance(build_detector("Mariana", ["Mari"]), NameTriggerDetector)
    assert isinstance(build_detector("", ["Mari"]), NameTriggerDetector)
    none = build_detector("", [])
    assert isinstance(none, NoTrigger)
    assert none.feed(SpeechLine(T0, Channel.OTHERS, "Mariana, o que acha?")) is None


def test_agent_brain_follows_the_backend() -> None:
    from sombra.brain.codex import CodexBrain

    kw = {"user_name": "Mariana", "aliases": ["Mari"], "allowed_topics": ["vendas"]}
    brain = build_agent_brain("claude", lambda: "k", level=AutonomyLevel.L1, model="sonnet", **kw)
    assert isinstance(brain, ClaudeBrain)
    assert brain.settings.model == "claude-sonnet-5-5"
    assert brain.settings.level is AutonomyLevel.L1
    assert tuple(brain.settings.aliases) == ("Mari",)
    codex = build_agent_brain("codex", None, level=AutonomyLevel.L2, model="sonnet", **kw)
    assert isinstance(codex, CodexBrain)
    with pytest.raises(ValueError, match="backend"):
        build_agent_brain("gemini", None, level=AutonomyLevel.L2, model="x", **kw)


def test_agent_model_and_key_per_backend() -> None:
    assert agent_model("claude", "opus") == "claude-opus-5-5"
    assert agent_model("codex", "sonnet") is None  # a Claude alias: Codex's own default
    assert agent_model("codex", "claude-opus-5-5") is None
    assert agent_model("codex", "gpt-5-codex") == "gpt-5-codex"
    assert key_provider("claude") == "anthropic"
    assert key_provider("codex") == "openai"


def _event() -> TriggerEvent:
    return TriggerEvent("t1", T0, "Mariana?", "Mariana", 0.9, (), False)


async def test_level_zero_stand_ins() -> None:
    brain, ui = NoBrain(), SilentUI()
    await brain.start(Path())
    with pytest.raises(RuntimeError, match="L0"):
        await brain.answer(BrainRequest(_event()))
    await brain.close()
    await ui.notify_trigger(_event())
    await ui.show(Suggestion("s", "t1", "x", "y"))
    await ui.notify_failure(_event(), "boom")
    actions = ui.actions()
    pending = asyncio.ensure_future(actions.__anext__())
    await asyncio.sleep(0.01)
    assert not pending.done()
    pending.cancel()


def _meeting(tmp_path: Path) -> Path:
    return create_meeting(tmp_path, "Planejamento", started_at=T0)


def test_one_marker_store_writes_each_tela_line_once(tmp_path: Path) -> None:
    meeting = _meeting(tmp_path)
    inner = MeetingStore(meeting, started_at=T0)
    store = OneMarkerStore(inner)
    record = FrameRecord("f0001", T0, "frames/f0001.jpg", 10, 10, "zoom.us", "Slide", 64.0, "0")
    store.append_frame(record)
    store.append_entry(FrameMarker(T0, "f0001", "Slide"))  # the session's echo: dropped
    store.append_entry(FrameMarker(T0, "f0001", "Slide"))  # a later, real re-mark: kept
    store.append_entry(SpeechLine(T0, Channel.OTHERS, "olha esse slide"))
    store.log(TriggerLogged("t", T0, T0, "q", "Mariana", 0.9, False))
    assert store.meeting_dir == meeting
    assert len(store.entries_since(T0 - timedelta(seconds=1))) == 3
    inner.close()
    lines = (meeting / "transcript.md").read_text().splitlines()
    assert lines.count('[14:30:00] TELA f0001 "Slide"') == 2
    assert len((meeting / "log.jsonl").read_text().splitlines()) == 1


class FakeModel:
    name = "fake-haiku"

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.calls: list[str] = []

    def complete(self, system: str, user: str, max_tokens: int) -> tuple[str, Usage]:
        self.calls.append(user)
        return self.texts.pop(0), Usage(input_tokens=10, output_tokens=5)


async def test_summary_hooks_write_epochs_and_minutes(tmp_path: Path) -> None:
    meeting = _meeting(tmp_path)
    store = MeetingStore(meeting, started_at=T0)
    store.append_entry(SpeechLine(T0 + timedelta(seconds=5), Channel.OTHERS, "o prazo é sexta"))
    minutes = {"resumo": "Prazo definido.", "decisoes": ["entrega na sexta"], "acoes": []}
    model = FakeModel(["Resumo: prazo na sexta.", "Resumo 2.", json.dumps(minutes)])
    epochs: list[str] = []
    now = T0 + timedelta(minutes=25)
    hooks = SummaryHooks(store, model, started_at=T0, on_epoch=epochs.append, clock=lambda: now)

    result = await hooks.summarize(1)
    assert result.model == "fake-haiku" and result.usage.input_tokens == 10
    assert "o prazo é sexta" in model.calls[0]
    hooks.epoch_started(1)
    assert epochs == ["Resumo: prazo na sexta."] == [hooks.summary]
    assert [e.text for e in read_epochs(meeting / "summary.md")] == ["Resumo: prazo na sexta."]

    await hooks.summarize(5)  # a numbering mismatch is only logged
    await hooks.write_minutes()
    assert "Prazo definido." in (read_minutes_section(meeting / "summary.md") or "")
    store.close()


async def test_assembly_close_keeps_going_after_a_failure(tmp_path: Path) -> None:
    meeting = _meeting(tmp_path)
    closed: list[str] = []

    async def bad() -> None:
        raise RuntimeError("boom")

    async def good() -> None:
        closed.append("good")

    store = MeetingStore(meeting, started_at=T0)
    assembly = Assembly(session=None, store=store, closers=[good, bad])  # type: ignore[arg-type]
    await assembly.close()
    assert closed == ["good"]


# --- live ------------------------------------------------------------------------------


def _plan(tmp_path: Path, level: AutonomyLevel, key: bool = False) -> LivePlan:
    meeting = _meeting(tmp_path)
    cfg = UserConfig(meetings_root=tmp_path, user=UserIdentity(name="Maria"))
    return LivePlan(
        meeting_dir=meeting,
        started_at=T0,
        config=cfg,
        level=level,
        api_key=(lambda: "k") if key else None,
        agent_key=(lambda: "k") if key else None,
    )


def test_live_brain_and_summary_follow_the_level_and_key(tmp_path: Path) -> None:
    assert isinstance(build_brain(_plan(tmp_path, AutonomyLevel.L0)), NoBrain)
    with pytest.raises(ValueError, match="auth set anthropic"):
        build_brain(_plan(tmp_path, AutonomyLevel.L2))
    keyed = _plan(tmp_path, AutonomyLevel.L2, key=True)
    assert isinstance(build_brain(keyed), ClaudeBrain)
    model = summary_model(keyed)
    assert isinstance(model, AnthropicTextModel) and model.name == "claude-haiku-4-5"
    assert summary_model(_plan(tmp_path, AutonomyLevel.L0)) is None
    codex = _plan(tmp_path, AutonomyLevel.L2)
    cfg = dataclasses.replace(codex.config, brain=BrainConfig("codex"))
    codex = dataclasses.replace(codex, config=cfg)
    assert type(build_brain(codex)).__name__ == "CodexBrain"  # `codex login` suffices


async def test_run_live_records_until_stopped(tmp_path: Path, socket_dir: Path) -> None:
    from sombra.audio import FileAudioSource

    plan = _plan(tmp_path, AutonomyLevel.L0)
    stop = StopSignal()
    pause = PauseController()

    def sources(p: LivePlan) -> tuple[FileAudioSource, ReplayScreen]:
        start = datetime.now().astimezone()
        audio = FileAudioSource(FIXTURE / "me.wav", FIXTURE / "others.wav", start=start)
        return audio, ReplayScreen(DirectoryScreenSource(FIXTURE / "frames"), start)

    async def stop_when_done() -> None:
        log = plan.meeting_dir / "log.jsonl"
        for _ in range(500):
            if log.read_text():
                break
            await asyncio.sleep(0.01)
        stop.request()

    stopper = asyncio.create_task(stop_when_done())
    await asyncio.wait_for(
        run_live(
            plan,
            stop,
            sources=sources,
            transcriber=fake_transcriber(),
            control_socket=socket_dir / "control.sock",
            pause=pause,
        ),
        timeout=20,
    )
    await stopper
    events = [json.loads(x) for x in (plan.meeting_dir / "log.jsonl").read_text().splitlines()]
    assert [e["type"] for e in events] == ["trigger"]  # L0: logged, never answered
    transcript = (plan.meeting_dir / "transcript.md").read_text()
    assert "OUTROS: Maria, você pode explicar este gráfico na tela?" in transcript
    assert not (socket_dir / "control.sock").exists()  # closed with the session


def test_stop_signal_requested_before_the_loop_binds() -> None:
    stop = StopSignal()
    stop.request()

    async def main() -> None:
        stop.bind()
        await asyncio.wait_for(stop.wait(), 1)

    asyncio.run(main())


def test_discard_meeting(tmp_path: Path) -> None:
    meeting = _meeting(tmp_path)
    discard_meeting(meeting)
    assert not meeting.exists()


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    """Short dir for Unix sockets: macOS caps AF_UNIX paths at 104 bytes, and pytest's
    ``tmp_path`` there (/private/var/folders/...) is already longer than that."""
    with tempfile.TemporaryDirectory(prefix="sb", dir="/tmp") as d:
        yield Path(d)


async def test_run_live_closes_what_it_opened_when_startup_fails(
    tmp_path: Path, socket_dir: Path
) -> None:
    plan = _plan(tmp_path, AutonomyLevel.L0)
    socket = socket_dir / "control.sock"

    def broken_sources(p: LivePlan) -> tuple[object, object]:
        assert socket.exists()  # the control socket was already listening
        raise RuntimeError("no microphone")

    with pytest.raises(RuntimeError, match="no microphone"):
        await run_live(
            plan,
            StopSignal(),
            sources=broken_sources,  # type: ignore[arg-type]
            transcriber=fake_transcriber(),
            control_socket=socket,
        )
    assert not socket.exists()


async def test_epoch_hook_failure_is_only_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = MeetingStore(_meeting(tmp_path), started_at=T0)

    def not_started(summary: str) -> None:
        raise RuntimeError("brain not started")

    hooks = SummaryHooks(store, FakeModel([]), started_at=T0, on_epoch=not_started)
    hooks.epoch_started(1)  # must not raise: Session logs the epoch after this hook
    assert "did not take the new summary" in caplog.text
    store.close()
