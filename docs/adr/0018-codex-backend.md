# ADR 0018: Codex backend through `codex exec`, confined by a read-only permission profile

- Status: **provisional**. The design and the confinement flags come from the Codex source and its SDK docs. The development container has no OpenAI key and no Codex CLI, so the live checks (answers, latency, cost, injection) wait for a human run. See [Left for a human](#left-for-a-human).
- Date: 2026-09-29
- Issue: #18 (PRD C5, C2, C6, "Codex em sandbox read-only")
- Numbering: this ADR uses the issue number, as ADR 0009 does.

## Context

C5 asks for a Codex backend behind the same `contracts.Brain` interface, selected by config, so the orchestrator never knows which agent answers. The PRD's integration section says Codex must run in a **read-only sandbox** with the meeting folder as its workspace. Gate 4 of CONTRIBUTING.md adds three rules: the agent gets read/grep/glob on the meeting folder only, meeting content is data, and images never enter history (C6).

Claude's backend (ADR 0009) runs its own tool loop on the Messages API. Codex is different: its agent loop lives in the CLI, and its only way to read files is a **shell tool** inside the CLI's sandbox. So confinement has to come from that sandbox.

What the Codex source says (openai/codex at `94d642d`, 2026-09-29; the same code ships in release `rust-v0.159.1`). The docs site (developers.openai.com) is blocked from the dev container, so these facts come from the source and the in-repo SDK README:

1. **`--sandbox read-only` allows reads anywhere.** It compiles to one filesystem entry, `:root = read` (`codex-rs/protocol/src/permissions.rs`, `read_only_file_system_entries`). Network is restricted and nothing is writable, but a shell command like `cat ~/.ssh/id_rsa` would succeed. That breaks C2.
2. **Permission profiles can narrow reads.** `default_permissions = "<name>"` plus `[permissions.<name>.filesystem]` entries (`read`, `write`, `deny`) and `[permissions.<name>.network] enabled = false`. The TypeScript SDK README documents passing them as raw `--config` overrides: `default_permissions` plus `permissions.<name>.filesystem={...}`. `":minimal"` adds the platform read roots a shell needs. On Linux those are `/bin`, `/sbin`, `/usr`, `/etc`, `/lib`, `/lib64` and the Nix store (`linux-sandbox/src/bwrap.rs`); on macOS they are the system paths in `seatbelt_read_only_platform_defaults.sbpl`. Neither set includes the home directory.
3. **A `--sandbox` flag disables profiles.** When `--sandbox` is set, `resolve_permission_config_syntax` returns `Legacy`, and a `default_permissions` override is ignored without warning (`codex-rs/core/src/config/mod.rs`).
4. **`codex exec` flags** (`codex-rs/exec/src/cli.rs`, `utils/cli/src/shared_options.rs`):
   - `--json`: prints JSONL events.
   - `--ephemeral`: persists no session files.
   - `--ignore-user-config`: skips `$CODEX_HOME/config.toml`; auth still works.
   - `--ignore-rules`, `--skip-git-repo-check`, `--cd DIR`, `--image FILE`, `--model`.
   - A prompt of `-` is read from stdin.
   - Approvals default to `never` in exec mode.
5. **Events** (`exec/src/exec_events.rs`): `turn.completed` carries `usage {input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens, reasoning_output_tokens}`. `input_tokens` *includes* cached tokens (`TokenUsage::non_cached_input`). Items: `agent_message`, `command_execution`, `file_change`, `web_search`, `mcp_tool_call`, `collab_tool_call`, `reasoning`, `todo_list`, `error`.
6. **Other config.** `CODEX_API_KEY` in the environment authenticates `codex exec` (`login/src/auth/manager.rs`). `web_search = "disabled"`, `features.view_image = false`, `project_doc_max_bytes = 0` (no `AGENTS.md`) and `developer_instructions` are ordinary config keys.

## Decision

1. **`CodexBrain` runs one `codex exec --json` process per answer**, through a `CodexRunner` seam; `SubprocessRunner` is the real one and uses no shell. The argv comes only from `build_argv`, and `sandbox_args` is always part of it:

   ```
   codex exec --json --color never
     --ephemeral --skip-git-repo-check --ignore-user-config --ignore-rules
     --cd <meeting>
     -c default_permissions="sombra"
     -c permissions.sombra.filesystem={":minimal"="read","<meeting>"="read"}
     -c permissions.sombra.network.enabled=false
     -c approval_policy="never"  -c web_search="disabled"
     -c features.view_image=false  -c project_doc_max_bytes=0
     -c developer_instructions="<system prompt>"  [-c model_reasoning_effort="low"] [--model M]
     [--image frames/fNNNN.jpg ...]  -
   ```

   **The "read-only sandbox" is this profile**, not `--sandbox read-only`. It has no `write` entry anywhere, and network is off. Unlike `--sandbox read-only`, it cannot read the user's home. We never pass `--sandbox`, because Codex would then drop the profile without warning (fact 3). Tests assert on every run that `--sandbox`, `--add-dir` and every `dangerously`/`yolo`/`full-auto` flag are absent. They also assert that the profile is exactly `{":minimal": "read", <meeting>: "read"}` with network disabled. The folder path is quoted as a TOML string, so no folder name can inject config.
2. **Defense in depth on top of the OS sandbox:**
   - The process gets a minimal environment (`PATH`, `HOME`, locale, `TMPDIR`, `CODEX_HOME`, plus `CODEX_API_KEY` from the keychain callable). Other secrets in Sombra's environment never reach the agent's shell.
   - The API key goes in the environment only, never in argv or the prompt.
   - A `file_change`, `web_search`, `mcp_tool_call` or `collab_tool_call` item voids the answer with `BrainSandboxError` (`kind = "sandbox_violation"`), even though the sandbox or config should already have stopped it.
   - Every `command_execution` is kept in `CodexBrain.last_run` for audit.
3. **Prompts come from `brain.prompt` (#8)** through the same `PromptKit` as Claude. `render_request` builds the Messages-shaped body; `flatten_request` then sends `system` as Codex **developer instructions** and the user content (context, epoch summary, transcript, tail) on **stdin**. Meeting content stays inside `<dados>` delimiters and never reaches the developer role. Image blocks are dropped from the text and the same frames go through `--image`; the `Imagem da tela fNNNN:` labels stay in the text in the same order.
4. **Images stay out of history (C6)** by construction:
   - Each answer is a new process with `--ephemeral`, so no session or rollout file is written, and no later process can see an earlier image.
   - Frames are passed by path from `frames/` and never copied elsewhere.
   - `features.view_image=false` stops the agent loading other images mid-turn.
   - A `PRECISO_DA_TELA fNNNN` reply gets one more run, with that frame added to the tail. That frame is reported in `frames_sent`, as with Claude.
5. **Usage is mapped to `contracts.Usage`** with Anthropic semantics:
   - `cache_read_input_tokens` = `cached_input_tokens`
   - `cache_creation_input_tokens` = `cache_write_input_tokens`
   - `input_tokens` = `input_tokens − cached − cache_write` (the uncached rest)
   - `output_tokens` = `output_tokens` (reasoning included)

   So `claude.cache_hit_rate` works unchanged for both backends.
6. **Selection by config.** `[brain] backend = "claude" | "codex"` in the user config (default `claude`). `sombra.brain.backend.create_brain(backend, ...)` builds the right `Brain`, and the orchestrator only ever sees `contracts.Brain`. `[models] agent` is a Claude alias, so it is not passed to Codex; `CodexSettings.model = None` lets the CLI choose its default model.

## Consequences

- **The OS minimum stays readable.** `:minimal` keeps system roots readable: `/etc` and `/usr` on Linux, the system paths on macOS. So `cat /etc/passwd` works, while `~/.ssh`, `~/Documents` and other meetings do not. These roots hold no user data and are world-readable anyway. Removing them would stop the shell from starting.
- **The agent has a shell**, confined as above, instead of our three tools. It can run `rg`, `cat` and `ls` in the folder but can't write, reach the network or leave the folder's read scope. Our `brain.tools` limits (no regex in grep, size caps) do not apply to Codex.
- **Process start is paid on every trigger.** The CLI's startup time counts against the 2–4 s LLM budget. There is no warm-up equivalent to Claude's `max_tokens: 0` request. OpenAI prompt caching is automatic on a byte-stable prefix. Codex's base instructions, tools and our developer instructions come first, then the append-only prefix, then the tail. Each run is a new thread, so the cache key differs per run; whether the prefix still hits the cache has to be measured.
- **Version dependency.** `default_permissions` profiles, `--ignore-rules` and `--ignore-user-config` must exist; they do in `rust-v0.159.1`. An older CLI that rejects a flag exits non-zero, which raises `BrainAPIError`, so it fails closed. An older CLI that doesn't know profiles would ignore them and fall back to its default exec sandbox. **Pin a recent CLI.** The network injection test below catches a regression.
- A failure never stops capture: every error is a `BrainError` subclass, which the orchestrator logs as `AgentErrorLogged` (invariant 5).
- **Revisit** if Codex ships an in-process SDK for Python with the same sandbox controls, or if measured startup latency breaks the budget. One alternative: point Codex at our `brain.tools` through an MCP server with the shell tool off (`features.shell_tool=false`).

## Left for a human

Needs the Codex CLI (`npm i -g @openai/codex` or the release binary) and an OpenAI key or `codex login`:

```sh
CODEX_API_KEY=... uv run pytest -m network tests/brain/test_brain_codex_network.py -s
CODEX_API_KEY=... ANTHROPIC_API_KEY=... \
    uv run pytest -m network tests/brain/test_brain_codex_network.py -s -k side_by_side
```

| Check | Result |
|---|---|
| 5-trigger fixture from #9 answers correctly (`test_five_triggers_answer_correctly`) | *pending human run* |
| p50 / p95 latency, Codex vs Claude (`test_side_by_side_with_claude`) | *pending human run* |
| Tokens (uncached / cache read / write / out) and cost per 5 answers, Codex vs Claude | *pending human run* (cost = tokens × the provider's price page on the day of the run) |
| Injection: canary in `~` never read, nothing written outside, no forbidden item (`test_injection_reads_and_writes_nothing_outside`) | *pending human run* |
| Codex CLI version used | *pending human run* |
