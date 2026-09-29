# Sombra

**An autonomous meeting participant that runs on your own machine.** Sombra listens to the call and watches the screen, keeps everything as one timeline in a folder, and answers for you when someone calls your name.

> Status: pre-MVP. The repository holds the scaffold, shared contracts and quality gates. The work is split into [GitHub issues](https://github.com/nickmaglowsch/sombra-call/issues). "Sombra" is a working name.

## How it works

```
 mic ──────┐                                      ┌──────────── meeting folder ────────────┐
           ├─► VAD + whisper.cpp ──► EU/OUTROS ──►│ transcript.md   (append-only timeline) │
 system ───┘        (local)                       │ frames/*.jpg + frames/index.jsonl      │
 audio                                            │ context/        (docs you pre-select)  │
                                                  │ summary.md      log.jsonl              │
 screen ──► every 5-10 s ──► dHash dedupe ───────►│ TELA f0123 "Zoom - Roadmap Q4" markers │
                                                  └───────────────────┬────────────────────┘
                                                                      │ grep / read (read-only)
 trigger detector: your name (fuzzy) + a question ──► orchestrator ──► agent (Claude Code / Codex)
                                                                      │
                                         approval overlay ◄───────────┘  approve · edit · discard
```

1. **Everything becomes text on one timeline.** Speech from your mic (`EU`) and from everyone else (`OUTROS`) is transcribed locally. Each kept screenshot adds a one-line marker to the same file.
2. **Images go to the AI only on demand.** Frames stay on disk. When a question depends on the screen ("what do you think of *this chart*?"), 1 to 3 frames ride along in the tail of that one call.
3. **Persistent transcript, ephemeral images.** The prompt prefix only grows and stays byte-identical between calls, so it hits the prompt cache. Images are never kept in history.
4. **Local first.** STT, VAD, dedupe and trigger detection run on your machine. The API only sees the context assembled for one answer.
5. **The agent searches; the orchestrator does not push.** The meeting folder is the agent's workspace; it greps and reads what it needs.
6. **Read-only by default.** In a meeting the agent cannot write outside the meeting folder, run commands or reach the network.

### Autonomy levels

| Level | Behaviour | Output | Phase |
| --- | --- | --- | --- |
| L0 | Record only; minutes and action items at the end | `summary.md` | MVP |
| L1 | Copilot: suggests an answer only to you | Private overlay | MVP |
| L2 | Answers when called, after your approval | Overlay with approve/edit | MVP (text), phase 3 (voice) |
| L3 | Answers alone | Virtual mic | Phase 4, gated on ≥50 L2 answers with ≥90% approved unedited |

## MVP targets

| Metric | Target |
| --- | --- |
| Latency, end of question → answer in overlay | p50 ≤ 6 s, p95 ≤ 10 s |
| Answers approved without edits | ≥ 70% |
| False triggers | ≤ 2 per hour |
| API cost per 1 h meeting | ≤ US$ 1 |
| Prompt-prefix cache hit rate | ≥ 80% |
| Sombra CPU during a call | ≤ 25% average |

`sombra report <meeting>` prints this table for recorded meetings; see [`docs/metrics.md`](docs/metrics.md).

## Platforms

MVP (phase 1) is **macOS on Apple Silicon**. Ubuntu with Wayland (GNOME) is phase 2; its feasibility spikes run in parallel with the MVP. Windows, X11-first and joining calls as a bot are out of scope.

## Repository layout

```
src/sombra/
  contracts/      shared file formats + module interfaces (the only cross-module import)
  <module>/       one package per module: store, audio, transcription, screen, trigger,
                  brain, ui, summary, privacy, metrics (added by their issues)
  orchestrator/   the only package that wires concrete modules together
  cli.py          `sombra` entry point
tests/            mirrors src/; hardware/network tests are marked and never run in CI
docs/
  ARCHITECTURE.md module map, meeting-folder layout, file formats, boundary rules
  adr/            architecture decision records
  spikes/         spike reports (one file per spike issue)
```

Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) before writing code.

## Development

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you). macOS modules also need Xcode command-line tools.

```sh
make setup   # uv sync
make check   # lint + format check + mypy --strict + tests with coverage (what CI runs)
make fmt     # auto-fix lint and formatting
uv run sombra --version
```

Configuration (user config, profiles, `sombra new`, `sombra profiles list`) is documented in [`docs/config.md`](docs/config.md).

After a meeting, `sombra ask latest "O que combinamos sobre o prazo?"` answers from the transcript with `[HH:MM:SS]` references; see [`docs/ask.md`](docs/ask.md).

Tests that need real devices, models, OS permissions or a real API are marked `hardware` or `network` and are skipped by default. Run them explicitly: `uv run pytest -m hardware`.

## Contributing

All work goes through issues and pull requests that pass the [quality gates](CONTRIBUTING.md). Issues are sized for one agent or person each and are built to run in parallel against the contracts in `src/sombra/contracts/`. Agents: start with [`AGENTS.md`](AGENTS.md).

## Privacy and consent

Sombra records other people and sends excerpts of their speech to an LLM provider. It asks you to confirm participants were told before a meeting starts, keeps frames for 7 days and transcripts for 30 days by default, stores API keys only in the OS keychain, and has a pause shortcut. This is not legal advice; review with counsel before using it in a company setting (LGPD).

## Product spec

The full PRD (in Portuguese) is at [PRD: Sombra, o participante de reunião autônomo](https://claude.ai/code/artifact/619c8be3-80e8-4a49-b15c-b1ef65cded0e). Requirement ids used in issues and code (A1, T3, S6, G4, C6, …) come from it.
