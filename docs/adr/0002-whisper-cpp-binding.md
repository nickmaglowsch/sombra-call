# ADR 0002: whisper.cpp through pywhispercpp, Silero VAD through onnxruntime

- Status: accepted
- Date: 2026-09-29
- Issue: #4 (T1, T2, T4)

## Context

`transcription` must turn two audio channels into `SpeechLine`s about 1 s after each
utterance ends, locally, with Metal on Apple Silicon. whisper.cpp is the engine (PRD). The
open choice is how Python talks to it, and how Silero VAD runs.

Options for whisper.cpp:

1. **`pywhispercpp`** (MIT): pybind11 binding with wheels for macOS arm64 and Linux
   x86_64/aarch64 for CPython 3.9-3.15; bundles whisper.cpp (ggml Metal is on by default
   for Apple builds).
2. **Long-lived `whisper-server` subprocess** over HTTP: isolates crashes, but we would
   have to build and ship the binary per platform, pay WAV encode + HTTP per utterance,
   and manage a process lifecycle and port.
3. **`whisper-cli` per utterance**: reloads the model every call (seconds). Rejected.
4. Other bindings (`whispercpp`, `whisper-cpp-python`): unmaintained or no current wheels.

Options for Silero VAD:

1. The `silero-vad` package: needs PyTorch (~1 GB) for a 2 MB model.
2. **The Silero v6 ONNX model through `onnxruntime`** (MIT), with the same framing as the
   upstream `OnnxWrapper` (512-sample frames + 64 samples of context, carried RNN state).
3. whisper.cpp's built-in VAD: runs only inside `whisper_full` on a whole buffer, so it
   cannot tell us *when* an utterance ended in a live stream.

## Decision

- **whisper.cpp via `pywhispercpp`**, one `Model` (whisper context) per channel.
  `whisper_full` releases the GIL (`py::gil_scoped_release` in its `main.cpp`), so the two
  channels' model calls run truly in parallel on their own threads, and the asyncio loop
  never runs model code. `no_context=True` so each utterance stands alone (fewer loops).
- **Silero VAD v6.2 ONNX via `onnxruntime`**, one CPU thread, one session per channel.
- Models are fetched by `scripts/download_models.py` into `~/.cache/sombra/models`, pinned
  to a Hugging Face commit / Silero git tag and checked by SHA-256 before use.
  `pywhispercpp`'s own downloader is never used (no hash check); we always pass a path.
- Default model `large-v3-turbo-q5_0`; `medium-q5_0` and `small-q5_1` as fallbacks, `tiny`
  for CI. Language `pt` by default, `auto` allowed.

## Consequences

- Two whisper contexts cost two copies of the weights (~550 MB each for
  large-v3-turbo q5_0), inside the 2 GB budget but not free. If memory becomes the
  constraint, a single shared context with a priority queue is the fallback, at the cost
  of one channel waiting for the other.
- A native crash in whisper.cpp takes the process down; the orchestrator must restart
  transcription. A `whisper-server` subprocess remains the option if that shows up in
  practice.
- Metal support depends on how the `pywhispercpp` macOS wheel is built. The hardware
  benchmark (`tests/transcription/test_transcription_benchmark.py`) is where that is
  confirmed; if the wheel turns out CPU-only, build from source with
  `CMAKE_ARGS="-DGGML_METAL=on" uv pip install --no-binary pywhispercpp pywhispercpp`.
