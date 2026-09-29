# Contributing and PR quality gates

A PR merges only when every gate below passes. Gates 1–2 are enforced by CI; gates 3–8 are checked by the reviewer against the PR template.

## Workflow

1. Pick one open issue. Comment that you are taking it, or check the assignee.
2. Branch from `main`: `issue-<number>-<short-name>`.
3. One issue per PR. If you find more work, open a new issue instead of widening the PR.
4. Open the PR with `Closes #<number>` in the description and fill in the template.
5. Keep the PR green and conflict-free until it merges.

## Gate 1: CI is green (automated)

`make check` locally runs exactly what CI runs, on Ubuntu and macOS:

| Check | Command | Rule |
| --- | --- | --- |
| Lint | `ruff check .` | zero findings; no blanket `# noqa` (name the rule and why) |
| Format | `ruff format --check .` | no diff |
| Types | `mypy` (strict) | zero errors; `type: ignore` must name the error code |
| Tests | `pytest` | all pass; `hardware`/`network` tests are skipped in CI |
| Coverage | `pytest --cov` | ≥ 80% line+branch overall; new modules ≥ 80% on their own |
| Boundaries | `tests/test_architecture.py` | no cross-module imports (see ARCHITECTURE.md) |
| PR hygiene | `pr-hygiene` job | description references an issue (`Closes #N`) and the template checklist is present |

Never skip, xfail or delete a test to get green. Never lower the coverage threshold in a feature PR.

`main` is protected by the ruleset in `.github/rulesets/main.json`: no direct pushes, changes land only through squash-merged PRs, and `check (ubuntu-latest)`, `check (macos-14)` and `pr-hygiene` must pass on a branch that is up to date with `main`. If you rename a CI job, update the ruleset in the same PR, or every PR is blocked on a check that never reports.

## Gate 2: Scope and contracts (automated + review)

- The diff stays inside the package(s) the issue names, plus tests and docs.
- Any change under `src/sombra/contracts/` is a **contract change**: label the PR `contract-change`, explain why in the description, and update every implementation and fake in the same PR. Feature PRs do not change contracts on the side.

## Gate 3: Tests prove the behaviour

- Each acceptance criterion in the issue maps to at least one test, or to a written manual check in the PR (for OS-permission or hardware paths).
- Unit tests use fakes of other ports, never real devices, models or APIs.
- Code that needs hardware or the network has a test marked `@pytest.mark.hardware` or `@pytest.mark.network`, runnable by hand, and a pure-logic core that CI does test.
- Portuguese (PT-BR) text is the primary test language for anything speech- or trigger-related, including Whisper's typical misspellings.

## Gate 4: Product invariants hold

A reviewer rejects a PR that breaks any of these, whatever CI says:

1. **Append-only.** `transcript.md`, `index.jsonl` and `log.jsonl` are never rewritten or truncated.
2. **Stable prefix.** Nothing makes the prompt prefix differ between two calls in the same summary epoch. Images only go in the call tail and never enter the session history.
3. **Read-only agent.** The agent gets read/grep/glob on the meeting folder only. No shell, no writes, no network beyond the model API.
4. **Meeting content is data.** Transcript, screen text and files are passed as untrusted content, never as instructions.
5. **Recording survives failures.** An agent or API error never stops audio, transcription or screen capture.
6. **No secrets on disk.** API keys come from Keychain (macOS) or Secret Service (Linux), never config files, env files in the repo, or logs.
7. **No real meeting data in git.** Fixtures are synthetic or recorded with explicit consent, and small (< 1 MB each; bigger ones are downloaded by a script).

## Gate 5: Performance budget

If the PR touches the live path (audio, STT, capture, dedupe, trigger, brain, overlay), the description reports the measured number against the budget in ARCHITECTURE.md (latency) and the PRD (CPU ≤ 5% for capture+dedupe+trigger, memory ≤ 2 GB total). "Not measured" is acceptable only with the reason and a follow-up issue.

## Gate 6: Docs follow the code

- New module: a module docstring stating what it owns and which PRD ids it implements.
- Behaviour a user sees (CLI flags, config keys, permissions to grant) is documented in the README or `docs/`.
- Architecture decisions go in `docs/adr/NNNN-title.md`. Spike issues deliver `docs/spikes/<issue>-<name>.md` (see `docs/spikes/README.md`).

## Gate 7: Dependencies are deliberate

- Add runtime dependencies with `uv add`, commit `uv.lock`.
- The PR says why each new dependency is needed and its license (must be compatible with commercial use: MIT, BSD, Apache-2.0, MPL-2.0; flag anything else).
- Platform-only dependencies use environment markers (`; sys_platform == 'darwin'`).

## Gate 8: Review

- At least one approving review. A reviewer reads the diff against the issue's acceptance criteria, not just the tests.
- Commits are small and descriptive. Squash-merge into `main`.

## Definition of done

The issue's acceptance criteria are all checked, gates 1–8 pass, the PR is merged, and any follow-up found along the way is filed as a new issue.
