# Summary: rolling epochs and minutes

Package `sombra.summary` (PRD C4, M3; autonomy level L0). The orchestrator decides *when* to call it; this package decides *what* gets written.

## `summary.md` layout

```markdown
# Resumo da reunião

## Época 1 · até 14:25:00
### Tópicos
...

## Época 2 · até 14:50:00
### Tópicos
...

## Ata

### Resumo
...
### Decisões
- ...
### Itens de ação
- [ ] Atualizar o roadmap no Notion — responsável: EU · prazo: amanhã · [14:08:30]
### Perguntas em aberto
- ...
```

- **Epoch sections** (`## Época N · até HH:MM:SS`) are appended once and never edited. Each one is a full rewrite of the meeting summary up to the last transcript line it covers (≤ ~400 words: topics, decisions, open questions, what people asked the user). The prompt prefix builder reads **only the latest epoch** with `sombra.summary.read_current_epoch(path)`, so the prefix changes once per epoch: a planned cache miss every 20–30 min.
- **`## Ata`** is always the last section. `sombra minutes` replaces it and keeps the epochs.
- `##`/`#` headings the model writes inside a section are demoted to `###`, so section boundaries stay unambiguous.

## Epochs

`EpochSummarizer(model, started_at=..., epoch_minutes=25)` (20–30 allowed). `due(now)` tells the orchestrator when a boundary has passed; `summarize(entries_since_last_boundary, now)` returns the new text, a `SummaryEpochLogged` for `log.jsonl` and the `EpochSection` to append. A failed call raises and changes nothing, so the orchestrator logs it and keeps recording. An epoch whose transcript is larger than the model context is folded chunk by chunk, carrying the running summary.

## Minutes

`write_minutes(meeting_dir, model)` reads `transcript.md`, asks the model for JSON (summary, decisions, action items with owner, due date and `ref` timestamp, open questions), and renders PT-BR markdown under `## Ata`. Every action item's timestamp must be a line of the transcript; items whose timestamp doesn't exist are dropped and logged (logger `sombra.summary.minutes`, level WARNING). A transcript larger than the model context is split into chunks (map), and the partial minutes are merged by the model (reduce, recursive if needed).

## `sombra minutes <meeting>`

Regenerates the minutes of a finished meeting. `<meeting>` is a folder path or a folder name under `~/Sombra/meetings/`.

```sh
uv run sombra minutes ~/Sombra/meetings/2026-09-29_1430_daily-time-x
uv run sombra minutes 2026-09-29_1430_daily-time-x --model claude-haiku-4-5
```

- Model: `claude-haiku-4-5` by default (cheap; minutes are not on the latency path), `--model` to change it.
- API key: read from the OS keychain (macOS Keychain / Linux Secret Service) with `keyring`, service `sombra`, user `anthropic-api-key` (`keyring set sombra anthropic-api-key`). If none is stored, the `ANTHROPIC_API_KEY` environment variable is used; that fallback is a **dev-only convenience** (manual `network` tests, machines without a keychain backend), not the supported setup. Keys never go in config files or logs.
- Exit codes: 0 written, 1 API or parse failure, 2 no `transcript.md`.

## The `TextModel` port

`summary` does not import `brain`. It calls `TextModel.complete(system, user, max_tokens) -> (text, Usage)`; `AnthropicTextModel(api_key=callable, model=...)` is the Messages API implementation. Meeting content goes only in the user turn, inside `<transcricao>`/`<resumo_anterior>`/`<parciais>` tags, and the system prompt tells the model it is data, not instructions. Any `<transcricao>`, `<resumo_anterior>` or `<parciais>` tag inside the content (any case or spacing) is rewritten to `‹…›` first, so meeting content cannot close the fence.
