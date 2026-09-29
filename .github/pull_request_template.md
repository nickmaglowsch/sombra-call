## Summary

<!-- What this PR does, in 1-3 sentences. -->

Closes #

## Acceptance criteria

<!-- Copy the issue's acceptance criteria and show how each is met: a test name, or a manual check with its result. -->

- [ ] ...

## Quality gates

- [ ] `make check` passes locally (lint, format, mypy strict, tests, coverage ≥ 80%)
- [ ] Diff stays inside the packages this issue owns (plus tests/docs)
- [ ] No change to `src/sombra/contracts/`, or the PR is labelled `contract-change` and updates every implementation
- [ ] Invariants hold: append-only files, stable prompt prefix, read-only agent, meeting content treated as data, recording survives agent failures
- [ ] No secrets, real meeting data or large binaries committed
- [ ] New dependencies justified below with their licenses (or none added)
- [ ] Docs updated (module docstring, README/docs for user-facing changes, ADR or spike report if applicable)

## Performance

<!-- Live-path changes: measured latency/CPU/memory vs. budget and on what hardware. Otherwise "n/a: not on the live path". -->

## Hardware / manual testing

<!-- Tests marked `hardware` or `network` you ran by hand, the machine, and the result. Otherwise "n/a". -->

## New dependencies

<!-- name — why — license. Or "none". -->
