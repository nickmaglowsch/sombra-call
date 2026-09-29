# ADR 0001: Python core with frozen contracts

- Status: accepted
- Date: 2026-09-29

## Context

The PRD leaves the core language open (Python for fast prototyping vs. Rust for a single binary). The MVP is being built by several agents in parallel, so interfaces and file formats must be fixed before modules exist.

## Decision

- Core in **Python 3.12**, managed with **uv**. Lint/format with ruff, types with `mypy --strict`, tests with pytest.
- Heavy work is native already: whisper.cpp, Silero VAD, ScreenCaptureKit / PipeWire. Python only glues it together, which keeps the < 5% CPU budget for capture, dedupe and trigger realistic.
- Shared file formats and module interfaces live in `sombra.contracts` and are changed only through `contract-change` PRs.
- Modules depend only on contracts; the orchestrator wires them (see `docs/ARCHITECTURE.md`).

## Consequences

- Distribution as a single binary is harder; revisit packaging (PyInstaller/briefcase, or a Rust rewrite of hot paths) after the MVP gate.
- Platform bindings (pyobjc, PipeWire/GStreamer via `gi`) often lack type stubs; they are listed in the mypy override and wrapped behind typed ports.
