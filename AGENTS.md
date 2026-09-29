# Instructions for coding agents

You are implementing one GitHub issue of Sombra, a local meeting agent. Several agents work in parallel on other issues, so staying inside your lane matters more than anything else.

1. Read `README.md`, `docs/ARCHITECTURE.md` and `CONTRIBUTING.md` first. The PRD ids in your issue (A1, T3, S6, G4, C6, …) are explained in the PRD linked from the README.
2. Work only in the package(s) your issue names, plus tests and docs. Import only `sombra.contracts` and your own package; `tests/test_architecture.py` fails otherwise.
3. Do not edit `src/sombra/contracts/`. If the contract really is wrong or missing something, stop and comment on the issue with the proposed change; it goes in a separate `contract-change` PR.
4. Code against the ports in `sombra.contracts.ports`. Where you need another module, write a small fake in your tests.
5. Keep platform bindings behind lazy imports so the package imports on every OS; mark tests that need devices, models, permissions or the network with `hardware` / `network`.
6. CLI commands go in your package's `commands.py` (see `src/sombra/cli.py`), even if the issue says `cli.py`. Add dependencies with `uv add`; on a `uv.lock` conflict take `main`'s file and rerun `uv lock`.
7. Run `make check` before every push. CI runs the same thing on Ubuntu and macOS.
8. Open the PR with `Closes #<issue>` and fill in every section of the PR template. Report measured numbers when you touch the live path.
9. When the issue asks for a decision (ADR) or a spike report, that document is part of the deliverable, not optional.
