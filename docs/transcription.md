# Transcription (`sombra.transcription`)

Local streaming speech-to-text for both channels (PRD T1, T2, T4). Design choices are in
[ADR 0002](adr/0002-whisper-cpp-binding.md).

## Models

Models are not in git. Download them once (about 600 MB for the default):

```sh
sombra models download                  # large-v3-turbo-q5_0 + Silero VAD
sombra models download small-q5_1       # a lighter fallback
sombra models list                      # everything known, with sizes, present/missing
sombra models download tiny --dir /tmp/models
```

The installer runs the first command for you ([install.md](install.md)). In a dev checkout,
prefix with `uv run`; `scripts/download_models.py` does the same.

Files land in `~/.cache/sombra/models/`. Each download is pinned to a fixed upstream
revision and checked against its SHA-256; a mismatch deletes the file and fails.

## Pipeline

Per channel (`EU` = mic, `OUTROS` = system audio), independently:

1. **Energy gates.** 32 ms frames below -60 dBFS, or within 10 dB of the channel's
   tracked noise floor, count as silence without running the VAD model. Measured on the
   dev container (Linux x86_64): 10 min of silence costs about 0.2 s CPU (0.03%).
2. **Silero VAD** scores the rest. Speech starts at probability ≥ 0.5 and an utterance
   closes after 500 ms below 0.35. Utterances under 250 ms are dropped. At 15 s an
   utterance is cut at the quietest frame of its last 3 s.
3. **whisper.cpp** transcribes each closed utterance on the channel's own thread and
   whisper context, so a long `OUTROS` turn never delays an `EU` line.
4. **Filter.** Non-speech tags (`[Música]`), loops ("sim sim sim sim …") and known
   hallucinations ("Legendas pela comunidade Amara.org", a lone "Obrigado.") are dropped.
5. The result is a `SpeechLine(ts=start of speech, channel, text)`.

## Settings

`TranscriptionSettings` (passed in by the orchestrator; the module reads no config file):

| Field | Default | Meaning |
| --- | --- | --- |
| `model` | `large-v3-turbo-q5_0` | key in `WHISPER_MODELS` (`medium-q5_0`, `small-q5_1`, `tiny`) |
| `models_dir` | `~/.cache/sombra/models` | where the downloaded files are |
| `language` | `pt` | ISO code, or `auto` to detect per utterance |
| `vocabulary` | `()` | names and acronyms; becomes Whisper's initial prompt (T4) |
| `n_threads` | `4` | whisper.cpp CPU threads per channel |
| `use_gpu` | `True` | Metal on Apple Silicon |
| `vad` | `VadSettings()` | thresholds, `min_silence_ms`, `max_utterance_s`, gates |

`WhisperTranscriber.from_settings(settings, on_stat=...)` builds the production pipeline;
`on_stat` receives a `SegmentStat` per utterance (end of speech, emit time, whisper time)
for latency logging.
