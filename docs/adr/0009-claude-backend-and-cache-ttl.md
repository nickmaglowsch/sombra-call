# ADR 0009: Claude backend on our own Messages API loop, 1-hour prompt cache

- Status: **provisional**. Based on the official docs and an offline cost model. It becomes accepted, or gets revised, when a human runs spike #9 with an API key (see `docs/spikes/9-prompt-cache.md`).
- Date: 2026-09-29
- Issue: #9 (PRD C2, C5, C6, C7, Spike 3)
- Numbering: this ADR uses the issue number so parallel PRs don't collide on `0002`.

## Context

When a trigger fires, Sombra has to answer in 2–4 s. It reuses a cached prompt prefix (system + context + epoch summary + transcript), sends 0–3 screenshots in the tail only, and must never let those images reach a later request (C6). The agent must be read-only on the meeting folder (C2). Triggers arrive at irregular gaps, typically 2–12 min apart. The PRD leaves two questions open:

1. **Agent SDK or own loop?** One option is a Claude Agent SDK session using the meeting folder as its workspace, with a throwaway fork per trigger so images don't stay in history. The other is our own tool loop on the Messages API, with local `read`/`grep`/`glob` tools restricted to the folder.
2. **Which cache TTL (C7)?** The 5-minute default or the 1-hour TTL.

Facts from the official docs (links in the spike report):

- Caching is a byte-exact prefix match over `tools → system → messages`. A 5-minute write costs 1.25× the base input price and a 1-hour write costs 2×. A read costs 0.1× on most models and **0.05× on Claude Opus 5.5** ($0.20/MTok). Every read refreshes the TTL. The minimum cacheable prefix on Opus 5.5 is 512 tokens.
- The TTL clock runs from the *start* of the request that last wrote or read the entry. With start-to-start gaps between 5 and 60 minutes, the docs recommend the 1-hour TTL, or a `max_tokens: 0` keep-alive just under 5 minutes.
- Agent SDK sessions persist the whole conversation to `~/.claude/projects/<cwd>/<id>.jsonl`. A fork copies the parent history into a *new* session file, and the original stays unchanged. The built-in `Read`/`Grep`/`Glob` tools take arbitrary paths. Confining them to one folder takes a `can_use_tool` callback or hooks, and `allowed_tools` auto-approves the tools it lists without calling that callback.

## Decision

1. **Use our own loop on the Messages API** (`sombra.brain.claude.ClaudeBrain`), not the Agent SDK.
   - We control every byte of the prefix: the tools come from a sorted, constant list, and the prefix comes from `brain.prompt` (#8) and is append-only. With the SDK, the harness owns the system preamble and tool definitions, and may change them across SDK/CLI versions.
   - Each answer is a fresh, stateless request built from the cached prefix plus a new tail. Nothing is carried over, so images reach no later request **by construction**, with no fork or cleanup to get right. The SDK approach also writes every fork, with its base64 frames, to a session file outside the meeting folder. That escapes our retention policy (frames 7 days) and the folder-only data model.
   - Read-only confinement is our own ~200-line module (`brain.tools`), tested directly for traversal, symlink and absolute-path escapes. There is no shell, write or web tool to disable. `grep` matches plain text (`|`-separated alternatives), not regular expressions: the model picks the pattern after reading untrusted meeting text, so a regex would open catastrophic backtracking (ReDoS). Tool calls run in a worker thread, so the 12 s deadline can always fire.
   - No subprocess per trigger. The SDK spawns the Claude Code CLI on each `query()`; its startup time lands on the latency budget. The spike harness measures this, pending a human run.
   - Usage fields (`cache_read_input_tokens`, `cache_creation_input_tokens`) come straight from each response and map 1:1 to `contracts.Usage`.
2. **Use the 1-hour cache TTL** (`ClaudeSettings.cache_ttl = "1h"`), set on the prefix breakpoint.
   - The offline model below uses the spike's trigger schedule (gaps 2–12 min). The 5-minute TTL misses the cache on every gap longer than 5 minutes: 6 of the 10 triggers, a 31% prefix hit rate, US$ 0.45/h on Opus 5.5. The 1-hour TTL keeps the prefix warm (83% prefix hit rate on answers 2–10) at US$ 0.23/h.
   - A 5-minute TTL plus keep-alive models slightly cheaper (US$ 0.19/h on Opus 5.5) because reads cost 0.05×. It needs a background timer that sends a request every 4.5 min of silence. That adds a moving part and rate-limit use to save about US$ 0.05/h, well inside the US$ 1/h budget. We don't take it now. It stays one setting away (`cache_ttl="5m"` plus an orchestrator timer) if the measured run shows a bigger gap.
3. **Defaults.** Model `claude-opus-5-5` (configurable), `effort: "low"` for latency (thinking can't be turned off on Opus 5.5), `max_tokens` 1024, a 12 s deadline for the whole answer, at most 4 tool rounds, and one retry on 529/503 within that deadline. Server-side refusal fallback (`fallbacks: "default"`) is on by default. A pre-warm `max_tokens: 0` request runs at `start()`.
4. **Backend seam (C5).** `ClaudeBrain` talks to a `ModelClient` protocol (Messages-shaped request in, content + `Usage` out). A Codex adapter can implement `contracts.Brain` itself, reusing `brain.prompt` and `brain.tools`, or provide a `ModelClient` that translates.

## Consequences

- Within one answer, the tool loop re-sends that answer's own images on each round, because the model needs them. The next answer never sees them. Tested in `test_images_never_reach_a_later_answer`.
- If the model's whole answer is a frame request (`PRECISO_DA_TELA fNNNN`, #8's prompt), the loop serves that frame from `frames/` as one more round instead of returning the request as the answer. Such frames, and frames pulled with `view_frame` or `read frames/fNNNN.jpg`, are reported in `frames_sent`.
- We don't get Claude Code's built-in agent features: subagents, compaction and its tuned tool descriptions. None of them are needed for a 1–3 sentence answer over one folder.
- The prefix hit rate falls below 80% only because of the transcript that grew since the last trigger. At 150 words/min, a 12-minute gap adds about 3.6k new tokens to write. A rolling summary epoch (C4) is the other planned miss.
- **Revisit** when the human spike run reports measured p50/p95 latency and cache numbers. Also revisit if it shows the Agent SDK fork is materially faster, or if Opus 5.5 at `low` effort misses the 2–4 s budget; the next candidate then is `claude-sonnet-5-5`, which the harness runs with `--model`.
