# ADR 0046: Claude Code CLI backend, paid by the user's Claude subscription

- Status: **provisional**. Every flag below was checked against the real CLI (`claude` 2.1.285, the current release on 2026-09-29) running the exact `build_argv` command against a local stand-in for the Anthropic API, which let us watch what the CLI sends and which tool calls it refuses. What needs a real Claude Pro/Max login (answers, latency, cache hits, usage windows) waits for a human run. See [Left for a human](#left-for-a-human).
- Date: 2026-09-29
- Issue: #46 (epic #43; PRD C5, C2, C6, C4, M3)
- Numbering: this ADR uses the issue number, as ADRs 0009 and 0018 do.

## Context

Epic #43 lets users pay for Sombra's agent with their own subscription, the way [Paseo](https://paseo.sh) does: Sombra launches the CLI the user already installed and logged in, and never handles the subscription credentials itself. ADR 0018 did this for Codex (`codex login`). This ADR does it for Claude: a `Brain` that runs `claude -p` headless, and `summary` text models that run `claude -p` and `codex exec` with no tools.

The Messages-API `ClaudeBrain` (ADR 0009) stays as the API-key option. The new backend must keep every Gate 4 invariant: read-only on the meeting folder, meeting content as data, images only in one answer's tail, and a failure never stopping capture.

What we checked on `claude` 2.1.285 (`--help`, the [CLI reference](https://code.claude.com/docs/en/cli-reference), [permissions](https://code.claude.com/docs/en/permissions), [permission modes](https://code.claude.com/docs/en/permission-modes), [authentication](https://code.claude.com/docs/en/authentication), [prompt caching](https://code.claude.com/docs/en/prompt-caching), the CHANGELOG, and runs against a local fake API):

1. **`--restricted`** (added in 2.1.248) removes the tools that run commands or code and `WebFetch`, keeps the file tools inside the working directory, refuses `bypassPermissions`, and ignores user, project and local settings files. Observed: `Read` of `~/secret.txt`, of `/etc/hostname`, of `<meeting>/../secret.txt` and of a symlink inside the folder that points outside were all refused ("… is outside <meeting>; --restricted confines the file tools to the working directory", "… resolves through a symlink to …"). `Grep` in the folder did not follow the symlink.
2. **`dontAsk`** denies every call that would otherwise prompt. File reads inside the working directory need no approval, so they still run. Observed without `--restricted`: reads, greps and globs outside the folder were all refused. `--permission-prompts none` (2.1.259) makes the "nobody answers prompts" part explicit.
3. **`blockReadsOutsideWorkingDirectories`** in `--settings` makes the file tools refuse paths outside the working directories in every permission mode. Observed: refused on its own too.
4. **`--tools Read,Grep,Glob`** is the whole built-in tool list the model sees (the `init` event reported `["Glob","Grep","Read"]`). `--disallowedTools` and `permissions.deny` add deny rules; deny beats allow at every level. A `Bash` call the fake API forced came back "No such tool available".
5. **`--setting-sources ""`** loads no user, project or local settings. With it, a `~/.claude/settings.json` hook and a `<meeting>/.claude/settings.json` hook did not run, and a `CLAUDE.md` and an `AGENTS.md` in the folder and in `~/.claude` never reached the request. `--settings '<json>'` still applies; managed (enterprise) settings always apply.
6. **`--strict-mcp-config --mcp-config '{"mcpServers":{}}'`** loads no MCP server (`mcp_servers: []` in `init`). **`--disable-slash-commands`** loads no skills (`skills: []`).
7. **`--no-session-persistence`** writes no session file under `~/.claude/projects/` (checked: none written for those runs, one written for a run without it).
8. **stream-json input** takes one `{"type":"user","message":{"role":"user","content":[…]}}` line per turn. Base64 `image` blocks pass through to the API unchanged. User `cache_control` markers pass through unchanged too. Two user lines make two model turns, not one.
9. **Cache breakpoints.** The CLI puts `cache_control` on its two system blocks and on the last block of the last one or two messages: 3 breakpoints on the first request, 4 on every tool round. The API allows 4.
10. **`--system-prompt-file`** replaces the CLI's default system prompt with ours (the CLI still prepends a one-line Agent SDK identity and a billing header). `--max-turns N` ends a run with `subtype: "error_max_turns"`.
11. **Output.** `init` reports `tools`, `mcp_servers`, `permissionMode`, `model` and `apiKeySource`. The last line is a `result` with `result` (the text), `is_error`, `api_error_status`, `usage` (Anthropic semantics: `input_tokens` is the uncached rest) and `permission_denials`. A 401 came back as `is_error: true, api_error_status: 401` with an assistant `error: "authentication_failed"`; a 429 as `api_error_status: 429`, `error: "rate_limit"`. Both exit 1.
12. **Auth precedence** (authentication docs): cloud-provider variables, then `ANTHROPIC_AUTH_TOKEN`, then `ANTHROPIC_API_KEY`, then `apiKeyHelper`, then `CLAUDE_CODE_OAUTH_TOKEN`, then the subscription login. In `-p` mode an API key in the environment is always used. `--bare` never reads the OAuth login, so it can't be used with a subscription.

## Decision

1. **`ClaudeCodeBrain` runs one `claude -p` process per answer** through a `ClaudeCodeRunner` seam; `SubprocessRunner` is the real one and uses no shell. The cwd is the resolved meeting folder. The argv comes only from `build_argv`, and `sandbox_args` is always part of it:

   ```
   claude -p --input-format stream-json --output-format stream-json --verbose
     --no-session-persistence --restricted
     --tools Read,Grep,Glob
     --disallowedTools Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,Agent,Skill,mcp__*
     --permission-mode dontAsk --permission-prompts none
     --setting-sources "" --settings '{"permissions":{"defaultMode":"dontAsk","allow":[],
         "deny":[<the same tools>, Read(~/.ssh), Read(~/.ssh/**), … ~/.aws ~/.gnupg ~/.config
                 ~/.claude ~/.claude.json ~/.codex ~/.netrc ~/Library/Keychains],
         "additionalDirectories":[],"disableBypassPermissionsMode":"disable",
         "blockReadsOutsideWorkingDirectories":true},
       "disableAllHooks":true,"enableAllProjectMcpServers":false}'
     --strict-mcp-config --mcp-config '{"mcpServers":{}}' --disable-slash-commands
     --max-turns 5 --system-prompt-file <private temp file> [--effort low] [--model M]
   ```

   **Why together they confine the agent.** The model sees three read-only tools and nothing else (`--tools`, `--restricted`, `--strict-mcp-config`, `--disable-slash-commands`). Three independent mechanisms refuse a read outside the folder: `--restricted`, `blockReadsOutsideWorkingDirectories` and `dontAsk`. Each one alone was enough in our checks, so a regression in one of them still leaves two. Nothing from the user's machine can widen the set: user and project settings, hooks, `CLAUDE.md`, MCP servers and skills are all off, bypass mode is disabled, and no `--add-dir` or allow rule adds a directory. The path deny rules on well-known secret stores are a fourth layer; a rule that would cover the meeting folder itself is dropped, since deny beats allow. Only enterprise-managed settings still apply, and they can only be set by an administrator.

   **Fail closed at run time.** Every run's `init` event is checked: tools must be a subset of Read/Grep/Glob, no MCP server, no plugin other than the CLI's own `…@builtin` ones, permission mode `dontAsk`. Any `tool_use` of another tool voids the answer. Each of these raises `BrainSandboxError` (`kind = "sandbox_violation"`). Denied calls are kept in `last_run.denials` and logged; the answer stands, because the CLI enforced the denial. Tests assert on every run's argv that no widening flag is present (`--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, `--add-dir`, `--allowedTools`, `--resume`, `--continue`, `--fork-session`, `--plugin-dir`, `--agents`, `--append-system-prompt`, `--permission-prompt-tool`, `bypassPermissions`, and so on).

2. **Version floor, fail closed.** `start()` runs `claude --version` and raises `BrainError` below `MIN_CLI_VERSION = 2.1.285` or when no version can be read, like `CodexBrain.start()`. The flags exist from 2.1.259, but 2.1.285 is the only version checked end to end, so we don't trust older ones. The CLI updates itself, so most users are already there. A newer CLI that rejects a flag exits non-zero (`BrainAPIError`); one that widens a default is caught by the `init` check.

3. **Prompt from `brain.prompt`**, through the same `PromptKit` as `ClaudeBrain` and `CodexBrain`. `render_request` builds the Messages body; `split_request` sends its `system` as the system prompt file and the user content (context, epoch summary, transcript, tail) on **stdin** as one stream-json user message. Meeting content stays inside `<dados>` delimiters in the user turn and never reaches the system prompt. The system prompt names the user and their topics, so it goes in a file in a private temp directory (0700, removed after the run), never argv. Nothing from the prompt is in argv.

4. **Images only in the tail, never in history (C6).** Frames are base64 `image` blocks in the tail, in the order `build_tail` puts them. Each answer is a new process with `--no-session-persistence`, so no session file with the images is written and no later process can see them. A `PRECISO_DA_TELA fNNNN` reply gets one more run with that frame added to the tail, as with Codex, and the frame is reported in `frames_sent`.

5. **Session strategy: a fresh session per answer, not `--resume`.** Resuming a session would need it saved under `~/.claude/projects/`, which would copy the transcript (and, if the tail were in it, the frames) out of the meeting folder and past our retention rules. Keeping images out of it would need a text-only parent session that grows by one model turn per transcript chunk, which costs a model turn and puts those turns in the prefix. A fresh, unsaved session per answer keeps invariant 2 by construction: the prefix is rebuilt from `brain.prompt`, byte-identical up to the new transcript lines.

6. **Cache strategy and expected hit rate.** We send **no `cache_control` of our own** (`split_request` drops `brain.prompt`'s markers). The CLI already uses all four breakpoints on every tool round (fact 9), so a fifth would make the API reject that request with a 400. What is cached is what the CLI marks: its system blocks (its identity line, the three tool definitions, our system prompt), about 1–3k tokens. These hit on every answer after the first while the cache is warm: the CLI uses a 1-hour TTL for the main conversation on a subscription (prompt-caching docs). The meeting prefix (context, epoch summary, transcript) is uncached on each answer, because the CLI's breakpoint sits after the tail, so no earlier write is a prefix of the next request. Within one answer, tool rounds do hit: the CLI's moving breakpoint caches the conversation so far. Expected per-answer prefix hit rate, from the #9 model: about 10–30% early in a meeting (a 3k cached head over a 5–20k prompt), falling as the transcript grows until a summary epoch (C4) shrinks it. That is well under the ≥ 80% target, which was set for API cost. On a subscription the cost is usage-window share and prefill latency, not dollars; the human run below measures both. **Revisit** if the CLI gains a documented way to place a breakpoint in user content that respects the 4-marker limit: marking the end of the prefix would restore ADR 0009's hit rate.

7. **Auth: subscription only, credentials untouched.** The child environment is an allowlist: `PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TMPDIR`, `CLAUDE_CONFIG_DIR`, plus `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and `DISABLE_AUTOUPDATER=1` (no telemetry, error reports or updates from inside a meeting). `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_OAUTH_TOKEN` and the cloud-provider switches never reach it: a key would silently move billing to the API (fact 12), and a base URL would redirect the traffic. If `init.apiKeySource` still reports a key source (an enterprise `apiKeyHelper`), we log a warning and carry on.

   **Credential stance.** Sombra launches the user's own CLI, on the user's machine, for the user's own use, the way a shell script would. It never reads, copies, stores or forwards the CLI's OAuth token or keychain entry. `HOME` and `CLAUDE_CONFIG_DIR` are passed only so the CLI can find its own login.

8. **Errors map to `BrainError`s**, so the orchestrator logs `AgentErrorLogged` and recording goes on (invariant 5):
   - `api_error_status` 401/403, `error: "authentication_failed"`, or "not logged in", "/login", "login expired", "invalid API key" → `BrainAuthError`, whose message says: run `claude` in a terminal and log in (`/login`).
   - 429, `error: "rate_limit"`, or "usage limit", "hit your limit", "resets …" → `BrainRateLimitError` (a `BrainError`).
   - 503/529 or "overloaded" → `BrainOverloadedError`; `error_max_turns` → `BrainError`; anything else, or no `result` line → `BrainAPIError`.
   - Timeout or cancellation kills the whole process group (`_kill_group` from `brain.codex`), so no helper outlives the answer holding the pipes. The 15 s default deadline covers process start, tool rounds and the frame round.

9. **Usage** comes from the `result` event, which already has Anthropic semantics, so `contracts.Usage` and `claude.cache_hit_rate` work unchanged. `BrainResponse.model` is the configured model, else the model the CLI reported, else `claude-code-default`.

10. **Summary models** (`sombra.summary.cli_models`), plain text in and out with **no tools at all**:
    - `ClaudeCliTextModel` runs `claude -p` with `--tools ""` (the request carried `tools: []`), `--restricted`, `dontAsk`, no settings sources, no MCP, no skills, `--max-turns 1`, `--no-session-persistence`, the system prompt in a private file, the user turn on stdin, and `CLAUDE_CODE_MAX_OUTPUT_TOKENS` set to the call's `max_tokens` (observed in the request). It runs in an empty private temp folder. `init` must report no tools; a `tool_use` or a `max_tokens` stop is an error.
    - `CodexCliTextModel` runs `codex exec` with ADR 0018's flags, a permission profile that reads only the OS minimum (`{":minimal"="read"}`, no folder), network off, in an empty temp folder, and every tool-adding feature disabled (`shell_tool`, `unified_exec`, `view_image`, `multi_agent`, `apps`, `plugins`, `browser_use`, `computer_use`, `image_generation`, `in_app_browser`, `tool_suggest`, `skill_search`, `sleep_tool`, `hooks`; names from `codex features list` on 0.159.1).
      **That does not give "no tools" on Codex 0.159.1.** Capturing the request against a local Responses endpoint shows the model is still offered `functions.exec` and `functions.wait` (code mode: JavaScript in a V8 isolate with no file system or network, whose nested tools are the now-disabled ones), `functions.request_user_input(_async)`, and the `collaboration.*` sub-agent tools (`spawn_agent`, `send_message`, `wait_agent`, …). Disabling *every* enabled feature, and the config keys we tried (`features.code_mode*`, `features.collaboration_modes`, `agents.max_depth=0`, `tools.code_mode`), removes none of them; `agents.max_threads=0` makes Codex exit. So on this version the tools cannot be removed from the request, and the adapter enforces "no tools" on the **output** instead: an allowlist (`CODEX_INERT_ITEMS` = `agent_message`, `reasoning`, `error`) where any other item type, started, updated or completed (a command, a file change, a code-mode or sub-agent call, or a type a newer Codex adds), voids the output with `CliModelError`. The OS sandbox still bounds what a call could reach before it is rejected: the OS minimum read-only, no folder, no network, and a sub-agent inherits the same profile. Codex has no output cap flag, so `max_tokens` is not enforced; the summary prompts bound the length. **Revisit** when Codex can turn these tools off; `CodexBrain` (ADR 0018) is offered the same extra tools, which is a follow-up outside this issue.
    - Both keep the `TextModel` interface, check the CLI version once, never pass an API key, and raise `CliModelError` subclasses (`CliAuthError` with the login fix, `CliRateLimitError`, `CliTimeoutError`). `summary` must not import `brain`, so the little process code is duplicated on purpose.

11. **Out of scope** (#47, R4): the config keys, the setup wizard, and wiring into `create_brain`, `start`, `replay`, `ask` and `sombra minutes`.

## Consequences

- **Subscription usage windows.** Pro and Max plans meter usage in rolling windows: a 5-hour session window, plus weekly limits. The CLI reports them in `rate_limit_event`s (`five_hour`, `seven_day` utilisation). A long meeting with many triggers, or other Claude use the same day, can exhaust the window mid-meeting. The answer then fails with `BrainRateLimitError`: the overlay shows the failure, the log records it, and recording, transcription, capture and summaries (unless they use the same CLI) go on. The uncached meeting prefix (decision 6) is what makes each answer weigh more against the window than on the API path.
- **Process start is paid on every trigger**, as with Codex. The CLI's startup plus an uncached prefill of the transcript count against the 2–4 s LLM budget. The human run measures it; if it breaks the budget, the Messages-API backend stays the low-latency option.
- **The system prompt is not only ours.** The CLI prepends a one-line Agent SDK identity and a billing attribution header to our system prompt. Neither changes the rules in our prompt, but the CLI owns those bytes and a new version may change them (one planned cache miss per update).
- **Managed settings still apply.** An administrator's managed settings can add permissions or hooks we can't turn off. The `init` check catches a wider tool set or permission mode; a managed `apiKeyHelper` is only logged.
- **Tool calls denied by the CLI don't void the answer.** The agent may try to read outside the folder after an injection; the CLI refuses and the model answers without it. The denial is logged, and meeting text stays inside `<dados>` as data.
- **Revisit** if the CLI gains a breakpoint control (decision 6), an in-process SDK with the same controls, or if the measured latency or usage-window cost breaks the budget.

## Left for a human

Needs `claude` ≥ 2.1.285 on `PATH`, logged in with a Claude Max (or Pro) subscription, and no `ANTHROPIC_API_KEY` in the shell:

```sh
SOMBRA_CLAUDE_LOGIN=1 uv run pytest -m network tests/brain/test_brain_claude_code_network.py -s
SOMBRA_CLAUDE_LOGIN=1 uv run pytest -m network tests/summary/test_summary_cli_network.py -s -k claude
SOMBRA_CODEX_LOGIN=1  uv run pytest -m network tests/summary/test_summary_cli_network.py -s -k codex
```

| Check | Result |
|---|---|
| Three answers on the #9 fixture are correct (`test_three_answers_latency_and_cache`) | *pending human run* |
| Latency per answer, p50 and max (same test) | *pending human run* |
| Tokens per answer (uncached / cache read / cache write / out) and hit rate (same test) | *pending human run* |
| `apiKeySource` is `none`: the subscription paid (same test) | *pending human run* |
| A read of a canary file in `~` is denied and never appears in the answer (`test_read_outside_the_folder_is_denied`) | *pending human run* |
| Summary text models answer on the subscription / `codex login` (`test_summary_cli_network.py`) | *pending human run* |
| `claude --version` used, and the plan (Pro / Max 5x / Max 20x) | *pending human run* |
