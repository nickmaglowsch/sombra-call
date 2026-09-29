# Architecture

This is the engineering reference every issue builds against. The product "why" is in the PRD (linked from the README); this file is the "how" and the rules.

## Module map

Each module is one package under `src/sombra/`, owned by one issue at a time.

| Package | Owns (PRD ids) | Implements (from `sombra.contracts`) |
| --- | --- | --- |
| `store` | meeting folder, templates, profiles, append-only writers (M1, M2, T3, S6 writing) | `TimelineStore` |
| `audio` | mic + system audio capture, device listing, reconnect (A1, A2, A3) | `AudioSource` |
| `transcription` | Silero VAD + whisper.cpp streaming, vocabulary prompt (T1, T2, T4) | `Transcriber` |
| `screen` | periodic capture, window metadata, window-only mode; dHash dedupe, resize, frame index (S1–S4) | `ScreenSource`, `FramePipeline` |
| `trigger` | fuzzy name match, request structure, 60 s window, deictics, cooldown (G1–G5) | `TriggerDetector` |
| `brain` | prompt assembly (stable prefix + ephemeral tail), agent backends, read-only sandbox (C1–C3, C5–C7) | `Brain` |
| `ui` | approval overlay and notifications (O1, O2) | `ApprovalUI` |
| `summary` | rolling summary epochs, end-of-meeting minutes and action items (C4, M3) | — |
| `privacy` | consent gate, pause shortcut, blocked apps, retention, keychain secrets | — |
| `metrics` | log analysis, success metrics, offline replay harness | — |
| `config` | user config file and meeting config schema | — |
| `orchestrator` | wiring, event loop, autonomy levels, failure handling, latency timestamps | — |

## Boundary rules (enforced by `tests/test_architecture.py`)

1. A feature package imports only `sombra.contracts` and itself. Never another feature package.
2. Only `orchestrator`, `cli` and `config` may import concrete implementations from several packages.
3. Modules receive settings as constructor arguments. They do not read the config file themselves.
4. Platform code lives in a submodule named after the platform (`audio/macos.py`, `screen/wayland.py`) behind the package's port. Importing the package on another OS must not fail; import platform bindings lazily.
5. Changing anything in `sombra.contracts` is a **contract change**: its own PR, labelled `contract-change`, with every implementation and fake updated in the same PR.

Because of rules 1–3, every module can be built and unit-tested in parallel against fakes of the other ports.

## Meeting folder

One folder per meeting, created from a template (M1). Default root: `~/Sombra/meetings/`.

```
2026-09-29_1430_daily-time-x/
  meeting.toml        name, started_at, user aliases, autonomy level, allowed topics, profile
  context/            files the user pre-selected (docs, tickets, notes); the agent reads, never writes
  transcript.md       the timeline; append-only, one entry per line
  frames/
    f0001.jpg         ~1280 px wide, JPEG q80
    index.jsonl       one FrameRecord per kept frame
  summary.md          written by the orchestrator only (rolling epochs, then final minutes)
  log.jsonl           triggers, suggestions, user actions, agent errors, usage
```

Writers: only the orchestrator (through `TimelineStore` and `summary`) writes to the folder. The agent is read-only on it.

## File formats

Defined in code, with round-trip tests, in `src/sombra/contracts/`:

- **`transcript.md` lines** (`contracts/timeline.py`):
  ```
  [14:32:07] EU: acho que dá pra fechar na sexta
  [14:32:09] OUTROS: Nick, o que você acha desse gráfico?
  [14:32:10] TELA f0123 "Zoom - Roadmap Q4"
  ```
  Local wall-clock `HH:MM:SS`. Lines are **never rewritten** (rewriting invalidates the prompt cache). The file may start with a header block written at creation; after that, only appends.
- **`frames/index.jsonl`**: one `FrameRecord` per line (`contracts/records.py`).
- **`log.jsonl`**: one event per line with a `type` of `trigger`, `suggestion`, `action`, `agent_error` or `summary_epoch`. These feed the success metrics and, later, the Laya classifier training set. Readers ignore unknown keys.

## Runtime flow (L2)

1. Audio chunks → `Transcriber` → `SpeechLine` → `TimelineStore.append_entry`.
2. Screenshots → `FramePipeline` → kept `FrameRecord` → `append_frame` + a `FrameMarker` line.
3. Every new entry → `TriggerDetector.feed`. On a hit: `TriggerEvent` (question, last 60 s, `needs_screen`, candidate frames).
4. Orchestrator logs the trigger, calls `ApprovalUI.notify_trigger`, then `Brain.answer` with 0–3 frame paths.
5. The brain sends *stable prefix* (system + context + epoch summary + transcript) + *ephemeral tail* (last 60 s, question, images). Images never enter the session history.
6. `ApprovalUI.show` → user approves, edits or discards → `ActionLogged`.
7. Agent/API failure → `AgentErrorLogged` + `ApprovalUI.notify_failure`; capture and transcription keep running.

## Latency budget (p50)

| Stage | Budget |
| --- | --- |
| End of speech → text (STT) | ~1 s |
| Trigger detection | ≤ 0.2 s |
| LLM answer (persistent session) | 2–4 s |
| **Total to text answer** | **≤ 6 s** |

A PR that touches a stage on this path reports its measured number.

## Decisions

See `docs/adr/`. Open PRD questions are decided in ADRs by the issue that needs the answer; until then, code against the port.
