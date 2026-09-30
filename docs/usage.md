# Using Sombra

From a fresh checkout to your first meeting, then the day-to-day commands. The MVP runs live on **macOS on Apple Silicon**. `sombra replay` runs anywhere.

## Install

Requirements: [uv](https://docs.astral.sh/uv/) and the Xcode command-line tools.

```sh
git clone https://github.com/nickmaglowsch/sombra-call && cd sombra-call
uv sync --extra window                                  # --extra window: the always-on-top overlay (pywebview)
uv run python scripts/download_models.py                # whisper large-v3-turbo (~570 MB) + Silero VAD
uv run sombra setup                                     # the agent: Claude or Codex, subscription or API key
```

Then write `~/.config/sombra/config.toml`. At least tell Sombra your name, so it knows when you're called (every key is in [config.md](config.md)):

```toml
autonomy_level = "L2"

[user]
name = "Mariana"
aliases = ["Mari"]        # other names people call you; they also go in Whisper's vocabulary
```

## Permissions (macOS)

With the default install (`Sombra.app`), macOS asks for these **for Sombra** the first time they are needed, and lists Sombra in **System Settings → Privacy & Security**. The grants stay across upgrades ([ADR 0050](adr/0050-macos-app-bundle.md)). With the uv install (`install.sh --no-app`), grant them to the app you run `sombra` from (Terminal, iTerm, VS Code…) and restart that app after granting. `sombra doctor` names the app that holds them ("permissions holder").

| Permission | Why |
| --- | --- |
| Microphone | your voice (`EU` lines) |
| Screen & System Audio Recording | screenshots, and the Core Audio process tap for the call audio (`OUTROS`) on macOS 14.4+ |
| Accessibility | the global pause shortcut (optional; `sombra pause` works without it) |

On macOS < 14.4, install a loopback driver such as BlackHole, route the call output through it and set `[audio] system = "BlackHole 2ch"`. Details: [macos-audio.md](macos-audio.md) and [macos-screen.md](macos-screen.md).

## Your first meeting

```sh
uv run sombra start "Daily time X"
uv run sombra start "Planning" --profile planning --level L1
uv run sombra start "Review" --window "Zoom Meeting"      # capture only that window (S4)
```

`sombra start` does the following, in order:

1. Loads your config and the profile.
2. Checks the whisper model is downloaded and the API key is in the Keychain. Nothing is created if either is missing (L0 runs without a key, but writes no minutes).
3. Creates the meeting folder under `~/Sombra/meetings/` and prints its path.
4. Shows the **consent notice**. Paste it into the call chat, then type `sim`. Any other answer removes the new folder and stops ([privacy.md](privacy.md)).
5. Warns if the disk is not encrypted (FileVault).
6. Starts capture (mic, call audio, a screenshot every `capture_interval_s`), local transcription and the trigger detector. At L1/L2 it also starts the Claude agent and opens the overlay. The overlay is an always-on-top window, or a browser tab if pywebview isn't installed or you pass `--no-window`.

When someone asks you something ("Mariana, o que você acha desse gráfico?"), the overlay shows *buscando contexto…*, then the suggested answer, the excerpt that fired it and any slide that was sent. You can approve, edit or discard it; every choice is logged.

**Stop** with Ctrl+C or by closing the overlay window. Sombra drains the transcription queue and writes the minutes (`## Ata` in `summary.md`), then prints the `sombra report` table. A second Ctrl+C while it stops aborts without waiting, and then the minutes are not written. If the audio devices hang while closing, Sombra gives up on them after 5 s and still writes the minutes (see [macOS audio](macos-audio.md#stopping)).

## Autonomy levels

| Level | What Sombra does | Needs |
| --- | --- | --- |
| `L0` | records and transcribes; logs triggers; minutes at the end | nothing (API key only for minutes) |
| `L1` | suggests answers privately in the overlay | name + API key |
| `L2` | suggests an answer when called; you approve/edit before using it | name + API key |
| `L3` | answers alone | not available yet (phase 4) |

`--level` overrides `autonomy_level` from the config for one meeting.

The agent that answers is `brain.backend` in the config, set by `sombra setup`: `claude-code` (your Claude Pro/Max plan through the Claude Code CLI), `claude-api` (the Anthropic key from the keychain) or `codex` (the Codex CLI, on `codex login` or an OpenAI key). The Codex agent only runs on a Codex CLI version whose tool set was verified, currently 0.159.1 (`npm i -g @openai/codex@0.159.1`); a newer or older CLI is refused with a message saying so, and `sombra setup` / `sombra doctor` say it first ([ADR 0018](adr/0018-codex-backend.md)). Summaries and minutes follow the agent unless `[summary] backend` says otherwise ([providers.md](providers.md)).

## Pause

| How | What |
| --- | --- |
| ⌃⌥⌘P | toggles pause (overlay window mode, Accessibility permission) |
| `sombra pause` / `sombra resume` | from any terminal, through the local control socket |

While paused, audio and screenshots are dropped before they reach the disk or the model.

## What's in the meeting folder

```
~/Sombra/meetings/2026-09-29_1430_daily-time-x/
  meeting.toml  consent.json  transcript.md  frames/  summary.md  log.jsonl  context/
```

The formats are described in [ARCHITECTURE.md](ARCHITECTURE.md#meeting-folder). You can regenerate the minutes later with `sombra minutes <meeting>` ([summary.md](summary.md)).

## Report

```sh
uv run sombra report ~/Sombra/meetings/2026-09-29_1430_daily-time-x
uv run sombra report --markdown --prices prices.toml <meeting>...
```

The report covers latency p50/p95, approved without edits, false triggers per hour, cost, cache hit rate and the L3 readiness line ([metrics.md](metrics.md)).

## Replay: the whole pipeline on recorded files

`sombra replay` feeds two WAV files (your mic, the call audio) and, optionally, a folder of screenshots through the same wiring as `start`. It uses the same VAD, whisper, dedupe, trigger detector, store and session, and writes a new meeting folder. It runs on any OS and needs no permissions. Replays reuse audio that was already recorded, so there is no consent prompt. Only replay files you are allowed to use.

```sh
uv run sombra replay me.wav others.wav --frames shots/ --user Mariana \
    --fake-brain --auto-approve --stt-model tiny           # offline, no API: what CI runs
uv run sombra replay me.wav others.wav --frames shots/ --speed 1    # real brain + overlay, real time
uv run sombra replay - others.wav --level L0                        # '-' = no file for that channel
```

| Flag | Meaning |
| --- | --- |
| `--frames DIR` | `<unix_ms>.png` screenshots, optional `<unix_ms>.json` sidecar `{"app": …, "window_title": …}`; the first one is shown when the audio starts |
| `--speed N` | pace audio and screenshots at N× real time; default: as fast as the models allow. `latency_ms` in `log.jsonl` is meaningful only at `--speed 1` |
| `--fake-brain` | a scripted answer instead of Claude (no API key, no minutes) |
| `--auto-approve` | approve every suggestion unedited instead of opening the overlay |
| `--user NAME` | the name to listen for (default: `[user] name`) |
| `--stt-model M` | whisper model (default: `models.stt`); `tiny` for quick checks |
| `--level`, `--name`, `--root`, `--config` | as for `start` |

The CI end-to-end test (`tests/e2e/test_e2e_replay.py`) is exactly this, on a synthetic PT-BR fixture generated by `tests/fixtures/e2e/make_fixture.py`.

## Troubleshooting

- **`model … not found`**: run `uv run python scripts/download_models.py` (add `tiny` for replays with `--stt-model tiny`).
- **`needs the … API key`**: run `uv run sombra setup` (or `sombra auth set anthropic` / `openai`), or record at `--level L0`.
- **`… is not logged in`**: log the CLI in again (`claude auth login` / `codex login`); `sombra doctor` shows the login state.
- **No `OUTROS` lines**: the call audio isn't reaching Sombra. Check the Screen & System Audio Recording permission, or the BlackHole routing on older macOS.
- **Screenshots missing for one app**: it may be on the blocked list (password managers, mail, banks; see [privacy.md](privacy.md#blocked-apps)).
