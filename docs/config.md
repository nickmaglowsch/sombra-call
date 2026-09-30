# Configuration

Sombra reads three kinds of TOML file. All of them are optional except `meeting.toml`, which `sombra new` writes for you.

| File | Purpose |
| --- | --- |
| `~/.config/sombra/config.toml` | user config (this page) |
| `~/.config/sombra/profiles/<name>.toml` | reusable context for a kind of meeting (M2) |
| `<meeting>/meeting.toml` | the settings of one meeting, written at creation |

`~/.config/sombra` becomes `$XDG_CONFIG_HOME/sombra` when that variable is set. The same path is used on macOS.

Errors always name the file and the key, for example `~/.config/sombra/config.toml: retention.frames_days: must be >= 1, got 0`. Unknown keys are errors, so a typo never falls back to a default without telling you.

**No secrets in config files.** Any key that looks like a secret (`api_key`, `*_api_key`, `token`, `*_token`, `secret`, `password`, `credential`, `private_key`) is rejected in every file. API keys live in the macOS Keychain or Linux Secret Service; set them with `sombra auth`.

## User config

A missing file means every default below.

```toml
meetings_root = "~/Sombra/meetings"   # where `sombra new` creates meeting folders
autonomy_level = "L1"                 # L0 record only, L1 private suggestions, L2 answer after approval, L3 answer alone
capture_interval_s = 5                # seconds between screenshots, 1-60

[user]
name = ""                             # your name; always one of the trigger aliases
aliases = []                          # other names people call you ("Nico", "Nicolas", ...)

[retention]
frames_days = 7                       # >= 1
transcripts_days = 30                 # >= 1

[audio]
# mic = "MacBook Pro Microphone"      # unset: system default input
# system = "BlackHole 2ch"            # unset: platform default loopback/tap

[models]
stt = "large-v3-turbo"                # whisper.cpp model
agent = "sonnet"                      # agent backend model alias
summary = "haiku"                     # rolling summary / minutes model alias

[brain]
backend = "claude-api"                # which agent answers: "claude-code", "claude-api" or "codex"
# auth = "subscription"               # codex only: "subscription" (`codex login`) or "api-key"

[summary]
backend = "follow"                    # summaries and minutes: "follow" [brain], a backend, or "none"
```

`sombra setup` writes `[brain]` and `[summary]` for you and keeps every other key and comment ([providers.md](providers.md)).

### Agent backend

`brain.backend` swaps the agent without touching anything else (PRD C5). `sombra setup` picks it, checks the CLI or key it needs, and asks one test question; [providers.md](providers.md) compares billing, limits and confinement.

- `claude-code`: your logged-in [Claude Code](https://code.claude.com/docs) CLI, paid by your Claude Pro/Max subscription (see below and [ADR 0046](adr/0046-claude-code-backend.md)).
- `claude-api` (default; `claude` is accepted as its old name): Claude over the Messages API with the Anthropic key from the keychain (`sombra auth set anthropic`), with read-only tools on the meeting folder ([ADR 0009](adr/0009-claude-backend-and-cache-ttl.md)).
- `codex`: the [Codex CLI](https://github.com/openai/codex) (`codex exec`), which must be on `PATH`. The agent runs only on 0.159.1, the version whose tool set was verified (#57); summaries accept 0.159.1 or newer. `brain.auth` says who pays: `"subscription"` uses your `codex login` (a ChatGPT plan) and never passes a key; `"api-key"` passes the OpenAI key from the keychain (`sombra auth set openai`). Without `auth`, the stored key is used when there is one, else `codex login`. Each answer runs under a Codex permission profile that can read only the meeting folder (plus the OS files a shell needs), write nothing and reach no network. Your `~/.codex/config.toml` is ignored for these runs. See [ADR 0018](adr/0018-codex-backend.md) and the residual risk in [providers.md](providers.md#codex-residual-risk).

`brain.auth` is only for `codex`; `claude-code` is always the subscription and `claude-api` always a key, and any other value is an error naming the file and key.

`[summary] backend` picks who writes the rolling summaries and minutes: `follow` (default) uses the agent's backend; `claude-code`, `claude-api` or `codex` pick one; `none` writes neither. The CLI summary models use the CLI's own login and never an API key.

`[models] agent` / `summary` are aliases per backend: `claude-api` maps `opus` / `sonnet` / `haiku` to API ids, `claude-code` passes them to the CLI, which resolves them for your plan, and `codex` ignores Claude names and uses its default (give a Codex model id to choose one). `"default"` lets every backend choose.

#### Claude Code CLI (Claude Pro/Max subscription)

`backend = "claude-code"` answers through your own installed and logged-in [Claude Code](https://code.claude.com/docs) CLI (`sombra.brain.claude_code.ClaudeCodeBrain`), so your Claude Pro or Max subscription pays for it instead of an API key. `[summary] backend = "claude-code"` (or `"codex"`, for a ChatGPT plan via `codex login`) does the same for rolling summaries and minutes (`sombra.summary.ClaudeCliTextModel` / `CodexCliTextModel`).

What you need:

- `claude` 2.1.285 or newer on `PATH` (`claude --version`; update with `claude update`). An older or unrecognised CLI is refused, because the confinement below depends on its flags.
- A subscription login: `claude auth login` (or run `claude` once and use `/login`); `sombra setup` offers to start it. `claude auth status` shows the login. If Sombra reports "Claude Code is not logged in", log in again.
- No API key. Sombra never passes `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_BASE_URL` to the CLI, even if they are set in your shell, because a key would move billing from the subscription to the API. Sombra never reads, copies or stores the CLI's login. `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) is not passed through either, so log in with `/login` rather than relying on that variable.

What each answer does:

- Runs `claude -p` in the meeting folder with only `Read`, `Grep` and `Glob`, confined to that folder (`--restricted`, `blockReadsOutsideWorkingDirectories`, `dontAsk`). There is no shell, no writing, no web, no MCP servers, no hooks, no skills or plugins from your settings, and your `~/.claude/settings.json` and any `CLAUDE.md` are not loaded.
- Sends the prompt and any screenshots on stdin, starts a fresh session each time and saves no session history (`--no-session-persistence`), so screenshots never end up under `~/.claude`.
- Uses your subscription's usage window. When the window or a rate limit is exhausted, the answer fails with a logged agent error and recording, transcription and screen capture carry on.

Details and the reasoning are in [ADR 0046](adr/0046-claude-code-backend.md).

## Profiles

A profile is a reusable set of context files and allowed topics, for example one per recurring meeting.

```toml
# ~/.config/sombra/profiles/planning.toml
description = "Weekly planning"
context = ["~/work/roadmap.md", "tickets/"]   # files or directories; relative paths resolve against the profiles dir
allowed_topics = ["roadmap", "deadlines"]
```

## Commands

```sh
sombra new "Daily time X"                    # prints ~/Sombra/meetings/2026-09-29_1430_daily-time-x
sombra new "Planning" --profile planning     # also copies the profile's context into context/
sombra new "x" --config ./other-config.toml  # use another user config file
sombra profiles list                         # one profile per line: name<TAB>description
sombra config init                           # write config.toml with every default, if there is none
sombra config path                           # print the user config path
sombra setup                                 # choose the agent provider (providers.md)
```

`sombra new` creates the folder described in [ARCHITECTURE.md](ARCHITECTURE.md#meeting-folder): `meeting.toml`, `context/`, an empty `transcript.md`, `frames/index.jsonl` and `log.jsonl`. If the folder name is already taken, it adds `-2`, `-3`, and so on.

## `meeting.toml`

Written once by `sombra new`; read by `config.load_meeting_config`.

```toml
name = "Planning"
started_at = 2026-09-29T14:30:05-03:00
aliases = ["Nick", "Nicolas"]
autonomy_level = "L1"
allowed_topics = ["roadmap"]
profile = "planning"                  # absent when no profile was used
context_files = ["context/roadmap.md"]
```
