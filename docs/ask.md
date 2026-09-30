# Asking about a past meeting: `sombra ask`

PRD use case "Depois da reunião": ask a question about a recorded meeting and get an answer with `[HH:MM:SS]` references to the transcript.

```sh
sombra ask latest "O que combinamos sobre o prazo?"
sombra ask "Daily time X" "Quem ficou com a migração?"          # most recent meeting with that name
sombra ask daily-time-x "..."                                   # or its slug
sombra ask 2026-09-29_1430_daily-time-x "..."                   # a folder name under meetings_root
sombra ask ~/Sombra/meetings/2026-09-29_1430_daily-time-x "..." # or a path
sombra ask latest "O que estava no gráfico?" --frames           # let the agent look at screenshots
```

| Option | Meaning |
| --- | --- |
| `--frames` | the agent may view screenshots (`frames/fNNNN.jpg`) by their `TELA` id. Without it, no image is sent to the model; the agent only sees window titles. |
| `--model` | model id or alias (`opus`, `sonnet`, `haiku`); default `[models] agent` from the user config |
| `--config` | user config file (default `~/.config/sombra/config.toml`); `meetings_root` and `[user]` come from it |

The answer goes to stdout; the meeting folder, model, elapsed time, frames viewed and token usage go to stderr. Exit codes: `0` answered, `1` the agent or API failed, `2` bad arguments (unknown meeting, empty question, invalid config, no API key).

## How it answers

The configured brain (`[brain] backend`: `claude-api`, `claude-code` or `codex`, see [providers.md](providers.md)) starts **read-only** on the meeting folder with a post-meeting system prompt (`brain/system_prompt_ask_pt.md`): the whole transcript is in the prompt, `summary.md` is added when it exists, and the agent can grep and read `transcript.md`, `summary.md` and `context/`. It is told to cite every claim with the `[HH:MM:SS]` of the transcript line and, when the records do not hold the answer, to start with "Isso não está na transcrição." As in a live meeting, transcript, summary and context are passed as untrusted data, never as instructions.

Without `--frames`, `claude-api` strips every image from the requests. The CLI backends (`claude-code`, `codex`) read files with their own tools, so they are started on a private temporary copy of the meeting folder without `frames/`, removed right after the question.

## Keys and logins

`sombra ask` uses the backend `sombra setup` configured. `claude-api` reads the Anthropic key from the OS keychain (`sombra auth set anthropic`), and `codex` with `auth = "api-key"` reads the OpenAI key (`sombra auth set openai`). `claude-code` and `codex` with `auth = "subscription"` use the CLI's own login and never get a key. The `ANTHROPIC_API_KEY` environment variable is **not** read any more (#40): keys live only in the keychain, as for `sombra start`. Never put a key in a config file; they reject secret-looking keys.

## Nothing is written

`sombra ask` does not write to the meeting folder, `log.jsonl` included. The existing `log.jsonl` event types describe live triggers; logging a question asked later as a `trigger`/`suggestion` would count as a live answer in `sombra report` and stretch the meeting's duration to the time of the question. Logging Q&A needs its own event type, which is a contract change.
