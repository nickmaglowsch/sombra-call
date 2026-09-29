# Meeting metrics

`sombra report` turns meeting folders into the PRD success-metric table, so every real meeting shows whether the MVP gate passes and how close L3 is. It only reads the folder (`log.jsonl`, `frames/index.jsonl`, `transcript.md`, and `started_at` from `meeting.toml`); it never writes to it.

```sh
sombra report ~/Sombra/meetings/2026-09-29_1430_daily-time-x          # table
sombra report m1 m2 m3 --prices prices.toml                           # per meeting + aggregate
sombra report m1 --markdown >> pr-body.md                             # paste into a PR or doc
sombra report m1 --json                                               # for scripts
sombra report m1 --missed --alias Nick --alias Nik                    # missed-trigger review list
```

With no folder it reports the current directory. `--json` and `--markdown` are exclusive. The exit code is 0 even when metrics fail; 2 means a folder or the price table could not be read.

From Python: `sombra.metrics.analyze(meeting_dir, prices) -> MeetingReport`, `aggregate(reports) -> AggregateReport`, `missed_triggers(meeting_dir, aliases)`.

## Metrics

Each row shows target, value and `pass` / `fail` / `n/a` (`n/a`: no target, or no data to compute it).

| Metric | Target | How it is computed |
| --- | --- | --- |
| Latency p50 / p95 | ≤ 6 s / ≤ 10 s | `latency_ms` of each `suggestion` event (end of question → overlay), nearest-rank percentile |
| Answers approved without edits | ≥ 70% | suggestions with verdict `approve` ÷ verdicts `approve` + `edit` + `discard` |
| False triggers | ≤ 2 / h | suggestions with verdict `not_for_me`, per hour |
| Prompt-prefix cache hit rate | ≥ 80% | `cache_read / (cache_read + cache_creation + input)` summed over answer calls (`suggestion` usage); summary calls are a different prompt and are excluded |
| API cost | ≤ US$ 1 / h | every logged `Usage` (suggestions and `summary_epoch`) priced with the price table, per hour |
| Frames kept after dedupe | — | lines in `frames/index.jsonl`, per hour |
| Frames attached per trigger | mean —, max ≤ 3 | `len(frames_sent)` of each suggestion |
| Agent errors | — | count of `agent_error` events |

Definitions:

- **Verdict.** A suggestion's verdict is the strongest of its `action` events: `not_for_me` > `edit` > `approve` > `discard` (so an edit followed by an approve counts as edited). Suggestions with no action are *undecided* and count in no ratio.
- **Duration** (the "per hour" denominator) runs from the first to the last timestamp in the log, the frame index and the transcript. Transcript times are `HH:MM:SS`; their date and time zone come from `started_at` in `meeting.toml`, else from the earliest logged timestamp. Naive timestamps are read as local time.
- **Aggregate.** Across meetings the raw counts are summed (latencies pooled, durations added) and every metric recomputed; ratios are never averaged. The cost is unknown if it is unknown for any meeting.
- **L3 readiness.** `L3 readiness: <n>/50 L2 answers, <rate>% approved unedited (needs ≥ 90%)`, where *answers* are suggestions with verdict `approve`, `edit` or `discard` and the rate is the approved share. The log does not record the autonomy level, so run the report on L2 meetings when judging the L3 gate.

### Forward compatibility

Unknown event types, unknown action kinds and unknown keys are ignored (counted as "unknown events" in the notes). A line that cannot be parsed, such as a half-written last line after a crash, is skipped and counted as a "bad line"; it never fails the report.

## Price table

Prices are not hard-coded; pass a TOML file with `--prices`. Values are US$ per million tokens; every key is optional (default 0). `"*"` is an optional fallback for models not listed. Without a table, or when a model in the log has no price and there is no fallback, the cost is shown as unknown and the notes name the unpriced models.

```toml
# prices.toml — example values; copy the current ones from your provider's price page.
[models."claude-sonnet-5-5"]
input = 3.0
output = 15.0
cache_read = 0.3
cache_creation = 3.75

[models."*"]
input = 3.0
output = 15.0
```

The model key is the `model` string logged on each `suggestion` / `summary_epoch` event.

## Missed-trigger review

`--missed` lists `OUTROS` lines that mention one of the user's aliases (whole word, case- and accent-insensitive) but have no logged `trigger` within 30 s. Without `--alias`, the aliases that matched logged triggers are used, so pass Whisper's usual misspellings (`Nik`, `Nic`) explicitly. Many mentions are not questions for the user; label each line by hand as *missed* or *correctly ignored*. The missed-trigger rate is the labelled *missed* count per hour.

## Measured outside this package

**CPU usage (target ≤ 25% average during a call).** Sample the Sombra process during a real call, at least 30 minutes:

- macOS: `top -pid $(pgrep -f 'sombra') -l 0 -s 5 -stats pid,cpu,mem > cpu.log`, or record the process in Instruments' Activity Monitor template. Average the `%CPU` column (per core; divide by the core count if you report whole-machine share, and say which).
- Linux: `pidstat -u -r -p $(pgrep -f sombra | paste -sd,) 5 > cpu.log` (sysstat), then average `%CPU`.

Report the machine, the call length and the average next to the `sombra report` output.

**STT name error rate.** From `transcript.md`, take a sample of at least 50 `OUTROS` utterances where the user's name was actually spoken (listen back, or take a meeting where the name was said on purpose). Count how many render the name as one of the configured aliases. Error rate = utterances where the name was not recognisable as an alias ÷ sample size. Add frequent misspellings to the alias list and the Whisper vocabulary prompt.
