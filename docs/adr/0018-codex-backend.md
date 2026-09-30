# ADR 0018: Codex backend through `codex exec`, confined by a read-only permission profile

- Status: **provisional**. The design and the confinement flags come from the Codex source and its SDK docs. The development container has no OpenAI key, so the live checks (answers, latency, cost, injection) wait for a human run. See [Left for a human](#left-for-a-human). The tool set (#57) *was* verified against a real `codex-cli 0.159.1` with a request capture; see [Tools the model is offered](#tools-the-model-is-offered-57).
- Date: 2026-09-29 (tool-set amendment for #57: 2026-09-29)
- Issue: #18 (PRD C5, C2, C6, "Codex em sandbox read-only"); #57 (tools and the output allowlist)
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
5. **Events** (`exec/src/exec_events.rs`): `turn.completed` carries `usage {input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens, reasoning_output_tokens}`. `input_tokens` *includes* cached tokens (`TokenUsage::non_cached_input`). Items: `agent_message`, `command_execution`, `file_change`, `web_search`, `mcp_tool_call`, `collab_tool_call`, `reasoning`, `todo_list`, `error`. **Not every tool call becomes an item** (measured on 0.159.1, #57): a code-mode `exec` call and a `collaboration.spawn_agent` call both ran with no item in the JSONL. A shell call made from *inside* `exec` still shows up as `command_execution`.
6. **Other config.** `CODEX_API_KEY` in the environment authenticates `codex exec` (`login/src/auth/manager.rs`). `web_search = "disabled"`, `features.view_image = false`, `project_doc_max_bytes = 0` (no `AGENTS.md`) and `developer_instructions` are ordinary config keys.
7. **Tools come from the model catalog, not only from feature flags** (measured on 0.159.1, #57). Each model in the bundled catalog (`codex debug models --bundled`) has fields that add tools: `tool_mode = "code_mode_only"`, `multi_agent_version = "v1" | "v2"`, `experimental_supported_tools`, `supports_search_tool` and `apply_patch_tool_type`. Ten of the eleven bundled models, including the default `gpt-6.1-sol`, are `code_mode_only` with multi-agent. `--disable` flags don't override these fields; `model_catalog_json = "<file>"` replaces the catalog. The table in [Tools the model is offered](#tools-the-model-is-offered-57) has the details.

## Decision

1. **`CodexBrain` runs one `codex exec --json` process per answer**, through a `CodexRunner` seam; `SubprocessRunner` is the real one and uses no shell. The argv comes only from `build_argv`, and `sandbox_args` is always part of it:

   ```
   codex exec --json --color never
     --ephemeral --skip-git-repo-check --ignore-user-config --ignore-rules
     --cd <meeting>
     -c default_permissions="sombra"
     -c permissions.sombra.filesystem={":minimal"="read","<meeting>"="read"[,"<native codex binary>"="read"]}
     -c permissions.sombra.network.enabled=false
     -c approval_policy="never"  -c web_search="disabled"
     -c features.view_image=false  -c project_doc_max_bytes=0
     -c model_catalog_json="<private temp>/models.json"
     --disable goals  --disable multi_agent
     -c tools.experimental_request_user_input={enabled=false}
     --disable code_mode  --disable code_mode_only  --disable multi_agent_v2
     -c developer_instructions="<system prompt>"  [-c model_reasoning_effort="low"] [--model M]
     [--image frames/fNNNN.jpg ...]  -
   ```

   **The "read-only sandbox" is this profile**, not `--sandbox read-only`. It has no `write` entry anywhere, and network is off. Unlike `--sandbox read-only`, it cannot read the user's home. We never pass `--sandbox`, because Codex would then drop the profile without warning (fact 3). Tests assert on every run that `--sandbox`, `--add-dir` and every `dangerously`/`yolo`/`full-auto` flag are absent. They also assert that the profile is exactly `{":minimal": "read", <meeting>: "read"}` with network disabled, plus, on Linux only, the native `codex` binary file (decision 8). Every path is quoted as a TOML string, so no folder name can inject config.
2. **Defense in depth on top of the OS sandbox:**
   - The process gets a minimal environment (`PATH`, `HOME`, locale, `TMPDIR`, `CODEX_HOME`, plus `CODEX_API_KEY` from the keychain callable). Other secrets in Sombra's environment never reach the agent's shell.
   - The API key goes in the environment only, never in argv or the prompt.
   - **The JSONL output is checked against an allowlist (#57).** This replaces the earlier denylist of `file_change`, `web_search`, `mcp_tool_call` and `collab_tool_call`, which let every other type through. Allowed events are `thread.started`, `turn.started`, `turn.completed`, `turn.failed`, `item.started`, `item.updated`, `item.completed` and `error`. Allowed items are `agent_message`, `reasoning`, `command_execution` and `error` (a warning such as "model metadata not found", which carries only a message). Anything else voids the answer with `BrainSandboxError` (`kind = "sandbox_violation"`). That includes an unknown type, an item without a type, and an item that is only started. This is the second line of defence. Fact 5 shows why it can't be the first: code mode and sub-agent calls emit no item at all.
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

7. **The model is offered only the shell (#57).** `CodexBrain.start()` runs `codex debug models --bundled` and clears the tool-adding fields on every model (`confine_catalog`). The fields and their new values are `tool_mode = null`, `multi_agent_version = null`, `experimental_supported_tools = []`, `supports_search_tool = false` and `apply_patch_tool_type = null`. The result goes to `models.json` in a fresh `mkdtemp` folder (0700 folder, 0600 file) outside the meeting folder, and every run loads it with `model_catalog_json`. `close()` deletes it. This costs once per meeting, not per trigger: the command took a median of 0.057 s over 5 runs here (Linux x86-64), for a 659 kB catalog. If the command fails or its output isn't a non-empty catalog of objects with a `slug`, `start()` raises `BrainError`, and Codex never runs with its default tools. The flags from decision 1 remove what the catalog doesn't: goals, multi-agent v1 and `request_user_input`. `code_mode`, `code_mode_only` and `multi_agent_v2` are off already on 0.159.1; disabling them explicitly keeps a future default change from bringing those tools back. What remains is `exec_command` plus `write_stdin`. These make up the unified-exec shell, which runs under the permission profile and is the shell this ADR accepts. `tests/brain/test_brain_codex_capture.py` checks this with the real CLI on every run where `codex` is installed.

8. **On Linux the profile also reads the native Codex binary, the file only (#63).** Codex starts every shell command through bubblewrap, and inside the sandbox it runs its own native binary again before the command. `:minimal` mounts only the system roots, so with an npm install outside them (`/opt/node22`, `~/.npm-global`, nvm, pnpm) every command failed with `bwrap: execvp …/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex: No such file or directory`. That failed closed, but the agent couldn't read the meeting folder at all. `codex_read_paths` finds the binary when the brain starts, once per meeting:
   - `shutil.which` on the CLI (`[brain] executable[0]`, or `CodexSettings.codex_binary` when the executable is a wrapper), then `realpath`.
   - A native binary (release tarball, Homebrew) is used as is.
   - The npm Node shim (`<pkg>/bin/codex.js`, a `#!` script) gives the native binary it starts: `@openai/codex-linux-<arch>/vendor/<triple>/bin/codex`, nested in `<pkg>/node_modules`, hoisted next to `<pkg>` (a pnpm store symlink resolves the same way), or `<pkg>/vendor/<triple>/bin/codex`. Only Linux triples count, and a symlinked binary is followed to its target.
   - Node is **not** added. The shim runs outside the sandbox, and only the native binary runs inside it. Measured: the command works with just that binary file added.
   - The grant is `read` on that one **file**, never its folder. The first version of this fix granted the folder and refused only `/`, `~` and the meetings folder. The #68 review showed the gap: a release binary unpacked into `~/Downloads` let the shell `cat ~/Downloads/notes.txt`. Codex's profile accepts file entries, and bubblewrap binds a file as readily as a folder, so nothing next to the binary is ever mounted, wherever the binary lives. `test_release_binary_in_a_user_folder_exposes_nothing_next_to_it` checks this live. With a folder grant it leaks the file next to the binary; with the file grant it doesn't.
   - **macOS doesn't need this.** Seatbelt runs the command itself under `sandbox-exec` with the generated profile. Codex doesn't re-run its own binary inside that sandbox, so a Homebrew or npm prefix outside the Seatbelt read roots doesn't matter. `codex_read_paths` returns nothing there, and the profile stays `{":minimal", <meeting>}`. This comes from Codex's design; it hasn't been run on a Mac yet. `test_shell_reads_the_meeting_folder_and_nothing_else` checks it on the first capture run there (see "Left for a human").

## Consequences

- **Supported Linux installs (#63):** `npm i -g @openai/codex@0.159.1` under any prefix (`/usr`, `/usr/local`, `/opt/…`, `~/.npm-global`, nvm), pnpm global, the release binary in any folder, and Homebrew on Linux. Anything else, such as a wrapper script that isn't the npm shim, needs `codex_binary` pointing at the real CLI. Otherwise its shell fails closed and the agent answers from the prompt alone.
- **The OS minimum stays readable.** `:minimal` keeps system roots readable: `/etc` and `/usr` on Linux, the system paths on macOS. So `cat /etc/passwd` works, while `~/.ssh`, `~/Documents` and other meetings do not. These roots hold no user data and are world-readable anyway. Removing them would stop the shell from starting.
- **The agent has a shell**, confined as above, instead of our three tools. It can run `rg`, `cat` and `ls` in the folder but can't write, reach the network or leave the folder's read scope. Our `brain.tools` limits (no regex in grep, size caps) do not apply to Codex.
- **Process start is paid on every trigger.** The CLI's startup time counts against the 2–4 s LLM budget. There is no warm-up equivalent to Claude's `max_tokens: 0` request. OpenAI prompt caching is automatic on a byte-stable prefix. Codex's base instructions, tools and our developer instructions come first, then the append-only prefix, then the tail. Each run is a new thread, so the cache key differs per run; whether the prefix still hits the cache has to be measured.
- **Version dependency, fail closed.** `default_permissions` profiles, `--ignore-rules` and `--ignore-user-config` must exist; they do in `rust-v0.159.1`. An older CLI might ignore the profile and fall back to its default exec sandbox, which can read the whole disk. So `CodexBrain.start()` runs `codex --version` and raises `BrainError` below `MIN_CLI_VERSION = 0.159.1`, or when no version can be read. A newer CLI that rejects a flag exits non-zero, which raises `BrainAPIError`. The network injection test below catches a newer CLI that changes semantics.
- **The tool set is verified per CLI version, and newer CLIs are refused (#57).** A newer CLI can add a catalog field that brings back a tool, or a feature that isn't catalog-driven. Neither our overrides nor the allowlist would necessarily catch it. So `start()` also refuses any CLI above `MAX_TESTED_CLI_VERSION = 0.159.1`, the last version the capture was run on. The error names the pinned install command (`npm i -g @openai/codex@0.159.1`) and the capture test. The cost: every Codex upgrade breaks the Codex backend until a maintainer reruns `test_brain_codex_capture.py` on it and raises the cap. That test ignores the cap, so it can run on the new version. We accept this cost: the alternative is an agent with tools nobody has looked at, driven by untrusted meeting text. A `--disable` for a feature the CLI no longer knows also makes Codex exit non-zero, so a removed flag fails closed too.
- **A model outside the bundled catalog** (e.g. `[brain] model` set to a newer slug) runs on Codex's fallback metadata. On 0.159.1 that offers the same two shell tools, plus a `Model metadata … not found` warning item that we log.
- **Timeouts kill the whole process tree.** The CLI runs in its own session (process group). On timeout or cancellation, the group gets `SIGKILL`, so sandbox helpers and shell commands die with it. Otherwise an orphan holding the pipes would keep the answer waiting past its deadline.
- A failure never stops capture: every error is a `BrainError` subclass, which the orchestrator logs as `AgentErrorLogged` (invariant 5).
- **Revisit** if Codex ships an in-process SDK for Python with the same sandbox controls, or if measured startup latency breaks the budget. One alternative: point Codex at our `brain.tools` through an MCP server with the shell tool off (`features.shell_tool=false`).

## Tools the model is offered (#57)

Method, following the PR #55 review: `codex-cli 0.159.1` (`npm i -g @openai/codex@0.159.1`) ran the exact `CodexBrain.build_argv` output against a local fake Responses endpoint. The endpoint was set with `-c model_providers.fake={base_url="http://127.0.0.1:PORT/v1",wire_api="responses",env_key=…}` and `-c model_provider="fake"`. It logged each POST body. The table lists the tools in the body's `tools` array and in its `input[].type == "additional_tools"` item, with namespaces flattened. `CODEX_HOME` was empty, and the model was the CLI default (`gpt-6.1-sol`) unless noted.

| Configuration | Tools in the request |
|---|---|
| #18 argv (before this change) | `functions.exec` (JavaScript in a V8 isolate; its nested tools are `exec_command`, `write_stdin`, `apply_patch`, `get_goal`, `create_goal`, `update_goal`, `clock__curr_time`), `functions.wait`, `functions.request_user_input`, `functions.request_user_input_async`, `clock.sleep`, `collaboration.spawn_agent`, `followup_task`, `send_message`, `list_agents`, `interrupt_agent`, `wait_agent` |
| + any of: `--disable code_mode_host --disable goals --disable multi_agent --disable sleep_tool`; `--disable code_mode --disable code_mode_only`; `--disable multi_agent_v2`; `--disable multi_agent`; `-c code_mode.enabled=false`; `-c features.multi_agent_v2.enabled=false`; `-c sleep_tool.enabled=false` | unchanged |
| + catalog with `tool_mode`, `multi_agent_version`, `experimental_supported_tools` cleared | `exec_command`, `write_stdin`, `request_user_input`, `apply_patch`, `get_goal`, `create_goal`, `update_goal`, `tool_search` |
| + `supports_search_tool = false`, `apply_patch_tool_type = null`, `--disable goals` | `exec_command`, `write_stdin`, `request_user_input`, `multi_agent_v1.spawn_agent`, `send_input`, `resume_agent`, `wait_agent`, `close_agent` (these had been hidden behind `tool_search`) |
| + `--disable multi_agent` | `exec_command`, `write_stdin`, `request_user_input` |
| + `-c tools.experimental_request_user_input={enabled=false}`: **the argv in decision 1** | **`exec_command`, `write_stdin`** |
| Decision 1 argv, `--model gpt-5.5` | `exec_command`, `write_stdin` |
| Decision 1 argv, `--model sombra-unknown-model` (fallback metadata) | `exec_command`, `write_stdin` |

With the catalog's `shell_type` set to `shell_command`, `local` or `default`, and with `--disable unified_exec`, the shell was still `exec_command` + `write_stdin`, so we leave `shell_type` alone.

Forced tool calls (the fake endpoint's first reply is the call; the second request shows what the model got back):

| Forced call | #18 argv | Decision 1 argv |
|---|---|---|
| `exec` (custom tool, `text("hi")`) | ran (`Script completed … hi`); **no JSONL item** | `unsupported custom tool call: exec` |
| `collaboration.spawn_agent` | reached the handler; it failed only because `--ephemeral` leaves no rollout for the child; **no JSONL item** | `unsupported call: collaborationspawn_agent` |
| `exec` calling `tools.exec_command` | `command_execution` item | tool not offered |
| `exec_command {"cmd": "ls"}` | `command_execution` item | `command_execution` item |
| raw `local_shell_call` (from the PR #59 review's own probe) | — | dropped silently: nothing ran, no item, no follow-up request |
| `web_search_call` (same probe) | — | a `web_search` item, which the allowlist voids |

Found while doing this, out of #57's scope:

- **Linux, npm install under `/opt`.** Every shell command failed with `bwrap: execvp …/codex-linux-x64/…/bin/codex: No such file or directory`. The sandbox helper lives outside the `:minimal` read roots. This failed closed, but it meant the agent couldn't read the meeting folder. Fixed in #63 (decision 8). It was checked with both a temp `CODEX_HOME` and a persistent one under `~`, where Codex does create its `tmp/arg0` helper aliases.
- **The developer message lists Codex's bundled system skills** (`imagegen`, `openai-docs`, …, under `$CODEX_HOME/skills/.system`). No tool is attached to them, and the sandbox can't read that folder. They cost prompt tokens.

## Containment and residual risk (#57)

What actually stops an agent driven by untrusted meeting text, strongest first:

1. **The tools aren't offered.** Decision 7's catalog and flags leave only `exec_command` + `write_stdin` in the request (capture above; `test_brain_codex_capture.py`). A forced call to a removed tool gets `unsupported … call` and runs nothing.
2. **The permission profile.** Reads are limited to the meeting folder plus `:minimal`, nothing is writable, and there is no network. Every shell command runs under it. On #55 the reviewer also saw it refuse `apply_patch` writes to the cwd and to `$HOME` made from inside code-mode `exec`.
3. **The code-mode isolate has no file or network access.** This was observed on #55 before its tools were removed: `require`, `process`, `fetch` and `Deno` were undefined, and `import("fs")` was refused. It matters only if a future CLI brings `exec` back past layer 1.
4. **`--ephemeral`.** It is required for C6. On 0.159.1 it is also why a forced `spawn_agent` failed: the child finds no rollout. `test_ephemeral_is_always_passed` pins it.
5. **The JSONL allowlist.** This is a tripwire, not the enforcement. It voids the answer on any event or item it doesn't know, but it sees only what Codex reports. Code-mode `exec` and `spawn_agent` calls report nothing (fact 5).

Residual risk:

- **A newer CLI could offer a new tool.** It could come from a catalog field or feature we don't clear, or from one that emits no item, so layers 1 and 5 would both miss it. The mitigation is in code: `start()` refuses any CLI above `MAX_TESTED_CLI_VERSION` (0.159.1), so Sombra never runs an unverified tool set. Rerunning the capture test and raising the cap is the maintainer process for accepting a new version, not a user-side mitigation. If the cap is raised without that capture, or someone runs `codex` outside Sombra, only layer 2 (the permission profile) is designed to hold on an unverified version. Layers 3 and 4 were observed on 0.159.1 only.
- **The accepted shell remains.** The agent can run read-only commands in the meeting folder and read the world-readable `:minimal` roots (`/etc`, `/usr`). That was already accepted in #18. On Linux it can also read the native Codex binary file itself (decision 8): an executable, no user data.
- **A ChatGPT login isn't verified yet.** The remote catalog refresh might override `model_catalog_json`; the live check is in the table below.

## Left for a human

Needs the Codex CLI (`npm i -g @openai/codex` or the release binary) and an OpenAI key or `codex login`:

```sh
CODEX_API_KEY=... uv run pytest -m network tests/brain/test_brain_codex_network.py -s
CODEX_API_KEY=... ANTHROPIC_API_KEY=... \
    uv run pytest -m network tests/brain/test_brain_codex_network.py -s -k side_by_side
# #57 with a ChatGPT login: the refreshed catalog must not override ours
SOMBRA_CODEX_LOGIN=1 uv run pytest -m network tests/brain/test_brain_codex_network.py -s -k live_catalog
# offline, no key: the request capture (done for 0.159.1; required before raising MAX_TESTED_CLI_VERSION)
uv run pytest tests/brain/test_brain_codex_capture.py -v
```

| Check | Result |
|---|---|
| 5-trigger fixture from #9 answers correctly (`test_five_triggers_answer_correctly`) | *pending human run* |
| p50 / p95 latency, Codex vs Claude (`test_side_by_side_with_claude`) | *pending human run* |
| Tokens (uncached / cache read / write / out) and cost per 5 answers, Codex vs Claude | *pending human run* (cost = tokens × the provider's price page on the day of the run) |
| Injection: canary in `~` never read, nothing written outside, no forbidden item (`test_injection_reads_and_writes_nothing_outside`) | *pending human run* |
| Codex CLI version used (must be exactly 0.159.1 for now; `start()` refuses older and newer) | *pending human run* |
| Request offers only `exec_command` + `write_stdin` (`test_brain_codex_capture.py`, offline) | **passed** on 0.159.1, Linux x86-64 (default model, `gpt-5.5`, unknown slug; forced `exec` and `spawn_agent` refused) |
| Shell `cat`s a file in the meeting folder; a file in the folder next to it and `~` are unreadable (`test_shell_reads_the_meeting_folder_and_nothing_else`, offline, #63) | **passed** on 0.159.1, Linux x86-64, npm under `/opt/node22`, temp and persistent `CODEX_HOME`. Without the fix the same test fails with `bwrap: execvp …/bin/codex` |
| A release binary in a user folder (a `Downloads`-like temp folder, with `codex-resources`) exposes nothing next to it (`test_release_binary_in_a_user_folder_exposes_nothing_next_to_it`, offline, #68 review) | **passed** on 0.159.1, Linux x86-64. With the earlier folder grant the same test read the file next to the binary |
| Same capture on macOS (incl. the #63 shell test with a Homebrew or npm prefix) | *pending human run* |
| With a ChatGPT login, `codex debug models` + our catalog shows no tool fields, and a live answer asked to use `spawn_agent`/`exec` has no item off the allowlist (`test_live_catalog_and_run_offer_no_code_mode_or_subagents`) | *pending human run* |
