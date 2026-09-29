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
backend = "claude"                    # which agent answers: "claude" or "codex"
```

### Agent backend

`brain.backend` swaps the agent without touching anything else (PRD C5):

- `claude` (default): Claude over the Messages API, with read-only tools on the meeting folder ([ADR 0009](adr/0009-claude-backend-and-cache-ttl.md)).
- `codex`: the [Codex CLI](https://github.com/openai/codex) (`codex exec`), which must be on `PATH` (tested against `rust-v0.159.1`). It authenticates with the OpenAI key from the keychain, or with an existing `codex login`. Each answer runs under a Codex permission profile that can read only the meeting folder (plus the OS files a shell needs), write nothing and reach no network. Your `~/.codex/config.toml` is ignored for these runs. See [ADR 0018](adr/0018-codex-backend.md).

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
