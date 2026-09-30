# Agent providers: Claude or Codex, on a subscription or an API key

Sombra's agent (live answers, `sombra replay`, `sombra ask`) and its summaries (rolling summaries and minutes, `sombra minutes`) can run on four setups. Pick one with `sombra setup`; it is saved in `~/.config/sombra/config.toml` ([config.md](config.md#agent-backend)).

It works the way [Paseo](https://paseo.sh) does. For a subscription, Sombra launches the `claude` or `codex` CLI you installed and logged in yourself, and never sees, copies or stores that login. For an API key, the key lives only in the OS keychain (`sombra auth set`), never in a file.

## The backends

| `sombra setup` choice | `[brain]` | What runs | Who pays | Needs |
| --- | --- | --- | --- | --- |
| Claude, on your Claude Pro/Max plan | `backend = "claude-code"` | the Claude Code CLI, `claude -p` ([ADR 0046](adr/0046-claude-code-backend.md)) | your Claude subscription's usage | `claude` ≥ 2.1.285, logged in with your plan |
| Claude, on an Anthropic API key | `backend = "claude-api"` (old name: `"claude"`) | the Messages API ([ADR 0009](adr/0009-claude-backend-and-cache-ttl.md)) | your Anthropic API account, per token | `sombra auth set anthropic` |
| Codex, on your ChatGPT plan | `backend = "codex"`, `auth = "subscription"` | the Codex CLI, `codex exec` ([ADR 0018](adr/0018-codex-backend.md)) | whatever `codex login` is logged in with: your ChatGPT plan when you used "Sign in with ChatGPT" | `codex` 0.159.1 exactly (see below), `codex login` |
| Codex, on an OpenAI API key | `backend = "codex"`, `auth = "api-key"` | the Codex CLI, with the key in its environment | your OpenAI API account, per token | `codex` 0.159.1 exactly, `sombra auth set openai` |

**Codex versions.** The Codex agent runs only on a CLI whose tool set was verified, currently exactly 0.159.1. Older is refused, and so is newer, until a maintainer re-verifies it ([ADR 0018](adr/0018-codex-backend.md), #57). `sombra setup` installs that version (`npm install -g @openai/codex@0.159.1`), and `sombra doctor` flags any other. Summaries through Codex accept 0.159.1 or newer.

`backend = "codex"` without `auth` (a config from before `sombra setup`) keeps the old behaviour: the OpenAI key from the keychain when one is stored, otherwise `codex login`.

**Summaries and minutes** follow the agent by default (`[summary] backend = "follow"`): `claude-code` uses `claude -p` with no tools, `codex` uses `codex exec` with the tool features turned off, and `claude-api` uses the Messages API. Set `[summary] backend` to another backend, or to `"none"` for no summaries or minutes. The CLI summary models **never get an API key** ([ADR 0046](adr/0046-claude-code-backend.md)). With Codex on an API key, summaries through `codex` still need `codex login`; otherwise pick `claude-api` or `none` for them. `sombra setup` and `sombra doctor` check this.

**Models.** `[models] agent` and `[models] summary` hold an alias (`opus`, `sonnet`, `haiku`) or a model id:

- `claude-api` turns the alias into an API id (`sonnet` → `claude-sonnet-5-5`).
- `claude-code` passes it to `claude --model` as is; the CLI resolves aliases for your plan.
- `codex` ignores Claude aliases and ids, and uses Codex's default model; put a Codex model id there to choose one.
- `"default"` lets every backend choose.

## How billing works

- **`claude-code`.** Your Claude Pro or Max plan pays, through its usage windows: a rolling 5-hour session window plus weekly limits, shared with everything else you do with Claude that week. Sombra strips `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` and `CLAUDE_CODE_OAUTH_TOKEN` from the CLI's environment, so a key in your shell cannot move the bill to the API. If the CLI itself is logged in to an Anthropic Console account (`claude auth login --console`) or uses an `apiKeyHelper`, that account is billed per token instead. `sombra setup` and `sombra doctor` warn when `claude auth status` reports that. Each answer is a fresh CLI process that caches only the CLI's system blocks, not the meeting transcript, so on this backend an answer counts for more against the window than the ≥ 80% cache-hit target assumes ([ADR 0046](adr/0046-claude-code-backend.md) decision 6).
- **`claude-api`.** Per token on your Anthropic account, at the API price of the model you chose. The stable prompt prefix is cached with a 1-hour TTL, which is what the "≤ US$ 1 per 1 h meeting" target is built on. `sombra report` prints the tokens per meeting.
- **`codex`, subscription.** Your ChatGPT plan's Codex usage limits pay, shared with your other Codex use. If `codex login status` says "Logged in using an API key", the OpenAI API is billed instead, and `sombra setup` and `sombra doctor` warn about it.
- **`codex`, API key.** Per token on your OpenAI account. The key goes from the keychain into the environment of each `codex exec` process only (`CODEX_API_KEY`), never into argv, a file or a log.

Sombra adds no fee and has no server of its own: the traffic goes from the CLI or SDK on your machine straight to Anthropic or OpenAI. Their terms and data policies for your plan or account apply.

## When a limit is hit

Every backend fails the same way, and **recording always continues** (Gate 4.5):

- **An answer.** A usage-window or rate-limit error (`BrainRateLimitError`), an expired login (`BrainAuthError`) or any other agent error ends that one answer. The overlay shows the failure, `log.jsonl` records an `agent_error`, and audio, transcription and screen capture keep running. The next trigger tries again; Sombra does not retry or queue it.
  - `claude-code` recognises the CLI's 429s, `rate_limit` errors and "usage limit" or "hit your limit" messages.
  - `codex` recognises messages that mention 429 or a rate limit. Other wording from a ChatGPT usage limit shows up as a generic agent error with the CLI's message.
- **A summary epoch.** The failure is logged and that epoch has no summary; the next epoch covers the time since the last good one. If the minutes fail at the end, run `sombra minutes <meeting>` once the window resets.
- **`sombra ask`** exits 1 with the agent's error on stderr.

On a subscription, the agent and the summaries share one usage window when they use the same CLI. A long meeting with many triggers can use up the window, and then the minutes fail too. Putting summaries on another backend avoids that (`[summary] backend`).

## What the agent can reach

| Backend | Tools | Confinement |
| --- | --- | --- |
| `claude-api` | Sombra's own read/grep/glob on the meeting folder | enforced in Sombra's code (`brain.tools`) |
| `claude-code` | the CLI's `Read`, `Grep`, `Glob` | `--restricted`, `blockReadsOutsideWorkingDirectories` and `dontAsk` each refuse reads outside the meeting folder; no shell, web, MCP, hooks, plugins or user settings; the tool list is checked on every run ([ADR 0046](adr/0046-claude-code-backend.md)) |
| `codex` | only the shell (`exec_command`, `write_stdin`): the tool-adding catalog fields and features are switched off and the offered tools were captured on 0.159.1 | a permission profile that can read only the meeting folder plus the OS files a shell needs, write nothing and reach no network ([ADR 0018](adr/0018-codex-backend.md)) |

`sombra ask` without `--frames` sends no screenshot. On `claude-api` a filter strips images from the request. The CLI agents read files themselves, so they run on a private copy of the meeting folder with `frames/` left out; the copy is removed after the question.

### Codex residual risk

This is stated as is, from [ADR 0046](adr/0046-claude-code-backend.md) (decision 10) and [ADR 0018](adr/0018-codex-backend.md) ("Containment and residual risk"):

- **Summaries through Codex are not "no tools at all", and Sombra cannot make them so on Codex 0.159.1.** The model is still offered sandboxed JavaScript (`exec`/`wait`), `request_user_input` and the sub-agent tools, and no feature flag or config key removes them. Their calls do not appear in `codex exec --json` output, so Sombra cannot detect them.
- **What contains them.** The permission profile has no meeting folder, no writes and no network, and `--ephemeral` makes sub-agents fail. So meeting text that tries to steer the model can run sandboxed JavaScript, read the OS minimum and attempt writes, all of which are refused. It cannot read your files, reach the network or start a working sub-agent on that version. Sombra voids a run on any tool item Codex does report, but that is a tripwire, not the enforcement.
- **The Codex agent** (`CodexBrain`) no longer gets those tools (#57). Its model catalog is rewritten so that only the shell is offered, and a forced call to a removed tool runs nothing. The shell itself is the accepted risk: it can read the meeting folder and the world-readable OS files a shell needs (`/etc`, `/usr` on Linux; system paths on macOS). On Linux it can also read the folder of the Codex binary itself, which Codex needs to start the sandboxed shell (#63). It cannot read your home folder or other meetings, and it has no network. The JSONL allowlist voids an answer on any item it doesn't know, but that is a tripwire, not the enforcement.
- **Upgrades.** A newer Codex could bring back a tool that neither the catalog rewrite nor the allowlist catches. So the agent refuses any CLI newer than 0.159.1 until the capture test is rerun on it. Summaries do not refuse newer CLIs: their containment is the profile and `--ephemeral`. Re-check `--ephemeral` and the offered tools on every Codex upgrade.
- **A ChatGPT login isn't verified yet** for the catalog override (ADR 0018). A human run is pending.
- Choose `claude-code` or `claude-api` if this risk is not acceptable for your meetings.

## Setting it up

```sh
sombra setup                          # interactive: pick one of the four, fix what is missing
sombra setup --show                   # what is configured, CLI versions and login states
sombra setup --non-interactive --backend claude-code
sombra setup --non-interactive --backend codex --auth api-key --summary-backend claude-api
sombra doctor                         # the Agent section checks the configured backend
sombra doctor --live                  # ...and asks it one test question
```

`sombra setup`:

1. Finds `claude` and `codex` on `PATH` and reads their versions.
2. For a missing CLI, shows the official install command and runs it only if you say yes:
   - Claude Code: `curl -fsSL https://claude.ai/install.sh | bash`
   - Codex: `npm install -g @openai/codex@0.159.1`, the version the agent is verified on
3. For a CLI version Sombra can't use, offers `claude update` for an old Claude Code, or `npm install -g @openai/codex@0.159.1` for a Codex that is older or newer than the verified one.
4. Checks the login with the CLI's own status command, `claude auth status` or `codex login status`. It runs that command without any API key in its environment, so it sees the same login a meeting will use, and never prints its output (`codex login status` shows part of a key). If the CLI is logged out, it offers to run the CLI's own login in your terminal (`claude auth login` or `codex login`); the browser flow and the token stay between you and the CLI.
5. For API-key mode, runs `sombra auth set anthropic` or `sombra auth set openai`, a hidden prompt that stores the key in the keychain.
6. Writes `[brain]` and `[summary]` into `config.toml` in place: every other key and every comment stays as it was. A config that doesn't load is left alone, and setup tells you what to fix.
7. Asks one test question through the backend you chose, the same way `sombra ask` would, on a throwaway meeting folder. A missing login, an old CLI or a bad key shows up now rather than mid-meeting.

`--non-interactive` never prompts, installs or logs in. It writes the choice and exits 1 with the list of what is left. Exit codes: `0` configured and answering; `1` written, but something is left or the test question failed; `2` bad arguments or a config that doesn't load.

## Left for a human

These need real logins and are not run in CI:

- `sombra setup` on a Mac with a Claude Max login, then one `sombra ask`.
- The same with a ChatGPT login through `codex login`.
- The network tests listed in ADR 0046 and ADR 0018.

Latency and usage-window numbers for the subscription backends come from those runs; none are claimed here.
